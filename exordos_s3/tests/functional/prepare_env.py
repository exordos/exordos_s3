#!/usr/bin/env python3
"""Prepare an Exordos Core environment for exordos_s3 functional testing.

Assumes a bootstrapped Exordos Core (``exordos bootstrap -m core``) and does
everything else:

  1. Generate an SSH key pair, injected into the images that are built.
  2. Build the ``exordos_s3`` wheel and the elements, with the manifests
     pointing at the artifact server of step 3 instead of repo.exordos.com.
  3. Serve the build output: element repository + pip index for the plugin.
  4. Register that server as an element repository in the core.
  5. Install ``metapaas`` (official repo) and ``s3aas`` (local repository),
     then wait for the CP node, the element and the s3 API.
  6. Print the env vars the functional suite needs.

The HTTP server is detached on purpose: the manifests reference it, so it has
to outlive this script. Stop it with ``--cleanup``.

Usage::

    python prepare_env.py \\
        --output-dir /tmp/s3aas-build \\
        --endpoint http://10.20.0.2/api/core \\
        --username admin --password <pass>
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import typing as tp
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ELEMENT_NAME = "s3aas"
PACKAGE_NAME = "exordos_s3"
# PEP 503 normalized project name — the directory pip looks the package up in.
PACKAGE_INDEX_NAME = "exordos-s3"

# Layout of the served tree, as produced by `exordos build --output-dir`.
ELEMENTS_DIR = "exordos-elements"
INDEX_DIR = "simple"

SYSTEM_PROJECT_ID = "00000000-0000-0000-0000-000000000000"
METAPAAS_IAM_USER = "metapaas"
# The metapaas element generates its IAM user password into this core secret,
# in its own system project.
METAPAAS_SECRET_PROJECT_ID = "12345678-c625-4fee-81d5-f691897b8142"
METAPAAS_PASSWORD_SECRET = "metapaas_user_password"
LOCAL_REPO_NAME = "s3aas-local"
CP_NODE_NAME = "metapaas-cp"
CP_API_PORT = 8080


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    print(f"[prepare-env] {msg}", flush=True)


def _run(cmd: list[str], **kwargs: tp.Any) -> subprocess.CompletedProcess:
    _log(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, **kwargs)


def _default_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class Core:
    """Thin wrapper around the ``exordos`` CLI bound to one core."""

    def __init__(self, endpoint: str, username: str, password: str) -> None:
        # --no-check-updates keeps the "new version available" banner out of
        # the output that gets parsed as JSON below.
        self._password = password
        self._auth = [
            "--no-check-updates",
            "-e",
            endpoint,
            "-u",
            username,
            "-p",
            password,
        ]

    def run(self, args: list[str], check: bool = True, quiet: bool = False) -> str:
        cmd = ["exordos", *self._auth, *args]
        if not quiet:
            # Same command with the password redacted, for the log.
            shown = [a if a != self._password else "***" for a in cmd]
            _log(f"  $ {' '.join(shown)}")
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.stdout and not quiet:
            print(result.stdout, end="", flush=True)
        if check and result.returncode != 0:
            print(result.stderr, end="", file=sys.stderr, flush=True)
            raise subprocess.CalledProcessError(result.returncode, cmd)
        return result.stdout

    def json(self, args: list[str], quiet: bool = True) -> list[dict]:
        # Polled in loops, so quiet by default: only the parsed result matters.
        out = self.run([*args, "-o", "json"], check=False, quiet=quiet)
        # Be forgiving about anything the CLI prints before the payload.
        starts = [i for i in (out.find("["), out.find("{")) if i != -1]
        if not starts:
            return []
        try:
            data = json.loads(out[min(starts) :])
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else [data]


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _generate_ssh_key(key_dir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    key_dir.mkdir(parents=True, exist_ok=True)
    priv, pub = key_dir / "id_rsa", key_dir / "id_rsa.pub"
    if not pub.exists():
        _run(
            [
                "ssh-keygen",
                "-t",
                "rsa",
                "-b",
                "4096",
                "-f",
                str(priv),
                "-N",
                "",
                "-C",
                "exordos-test",
            ]
        )
    _log(f"SSH key pair: {priv}")
    return priv, pub


def _build_wheel(project_dir: str, dest: pathlib.Path) -> pathlib.Path:
    """Build the exordos_s3 wheel into ``dest`` and return its path."""
    # Start clean so a wheel left over from an earlier run cannot be picked up.
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    _run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dest)],
        cwd=project_dir,
    )
    wheels = sorted(dest.glob(f"{PACKAGE_NAME}-*.whl"))
    if not wheels:
        raise FileNotFoundError(f"No {PACKAGE_NAME} wheel built in {dest}")
    _log(f"Built wheel: {wheels[-1].name}")
    return wheels[-1]


def _wheel_version(wheel: pathlib.Path) -> str:
    """``exordos_s3-0.1.1.dev3+gabc-py3-none-any.whl`` -> ``0.1.1.dev3+gabc``."""
    return wheel.name.split("-")[1]


def _build_element(
    project_dir: str,
    output_dir: pathlib.Path,
    pub_key: pathlib.Path,
    manifest_vars: dict[str, str],
) -> None:
    cmd = [
        "exordos",
        "build",
        "-i",
        str(pub_key),
        "-f",
        "--output-dir",
        str(output_dir),
    ]
    for key, value in manifest_vars.items():
        cmd += ["--manifest-var", f"{key}={value}"]
    _run([*cmd, project_dir])


def _element_version(output_dir: pathlib.Path) -> str:
    """Read the built element version from its inventory."""
    inventories = sorted(
        (output_dir / ELEMENTS_DIR / ELEMENT_NAME).glob("*/inventory.json")
    )
    if not inventories:
        raise FileNotFoundError(f"No {ELEMENT_NAME} inventory under {output_dir}")
    data = json.loads(inventories[-1].read_text())
    if isinstance(data, list):
        data = data[0]
    return str(data["version"])


# ---------------------------------------------------------------------------
# Artifact server
# ---------------------------------------------------------------------------


def _start_http_server(root: pathlib.Path, port: int, pid_file: pathlib.Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--directory", str(root)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(1)
    if proc.poll() is not None:
        raise RuntimeError(f"HTTP server failed to start on port {port}")
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(proc.pid))
    _log(f"Artifact server: port={port} dir={root} pid={proc.pid}")


def _stop_http_server(pid_file: pathlib.Path) -> None:
    if not pid_file.exists():
        _log("No artifact server pid file, nothing to stop")
        return
    pid = int(pid_file.read_text().strip())
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        _log(f"Stopped artifact server (PID {pid})")
    except (ProcessLookupError, PermissionError):
        _log(f"Artifact server PID {pid} not running")
    pid_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Core interaction
# ---------------------------------------------------------------------------


def _register_repository(core: Core, url: str) -> None:
    """Register the local artifact server as an element repository."""
    if any(repo.get("name") == LOCAL_REPO_NAME for repo in core.json(["repo", "list"])):
        _log(f"Repository '{LOCAL_REPO_NAME}' already registered")
        return
    core.run(
        [
            "repo",
            "add",
            "-p",
            SYSTEM_PROJECT_ID,
            "-n",
            LOCAL_REPO_NAME,
            "--repo-url",
            url,
            "--priority",
            "4096",
        ]
    )


def _install_element(
    core: Core, name: str, version: str | None = None, retries: int = 5
) -> None:
    """Install an element, retrying on a busy core.

    The default covers transient API errors — a read timeout or a 502 while
    the core reconciles; a larger count covers a repository the core has not
    finished scanning yet.
    """
    cmd = ["ee", "install", name]
    # "latest" is not a version the CLI accepts: omitting --version means latest.
    if version and version != "latest":
        cmd += ["--version", version]
    for attempt in range(1, retries + 1):
        try:
            core.run(cmd)
            return
        except subprocess.CalledProcessError:
            if attempt == retries:
                raise
            _log(f"Element '{name}' not installable yet ({attempt}/{retries})")
            time.sleep(10)


def _wait_for_element(core: Core, name: str, timeout: int) -> None:
    _log(f"Waiting for element '{name}' to become ACTIVE…")
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    seen = ""
    while time.monotonic() < deadline:
        elements = core.json(["ee", "list", "-f", f"name={name}"])
        status = str(elements[0].get("status", "")) if elements else ""
        if status != seen:
            _log(f"  element '{name}': {status or '(not listed yet)'}")
            seen = status
        if status == "ACTIVE":
            _log(f"Element '{name}' is ACTIVE after {time.monotonic() - started:.0f}s")
            return
        if status == "ERROR":
            core.run(["ee", "show", name], check=False)
            raise RuntimeError(f"Element '{name}' entered ERROR state")
        time.sleep(15)
    # Leave something to read in the log instead of just the timeout.
    core.run(["ee", "show", name], check=False)
    core.run(["cn", "list"], check=False)
    raise TimeoutError(f"Element '{name}' did not become ACTIVE within {timeout}s")


def _wait_for_node(core: Core, name_pattern: str, timeout: int) -> str:
    """Wait for a compute node matching the pattern; return its IP address.

    Only the IP assignment is waited for here: `cn list` reports the declared
    state, which stays NEW even for nodes that are up — the bootstrapped core
    node itself included. Readiness is taken from the element and the API.
    """
    _log(f"Waiting for the IP of the node matching '{name_pattern}'…")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for node in core.json(["cn", "list"]):
            if name_pattern not in str(node.get("name", "")):
                continue
            ip = str(node.get("ip", "") or "").strip()
            if ip:
                _log(f"Node '{node.get('name')}' has IP {ip}")
                return ip
        time.sleep(15)
    core.run(["cn", "list"], check=False)
    raise TimeoutError(f"No node matching '{name_pattern}' got an IP within {timeout}s")


def _wait_for_api(url: str, timeout: int, expect_route: bool = False) -> None:
    """Wait until the URL answers.

    Any HTTP status means the API is serving — 401 included. With
    ``expect_route`` a 404 counts as "not yet": that is how the user-api
    answers for a plugin route it has not loaded.
    """
    _log(f"Waiting for {url} …")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                _log(f"{url} answered HTTP {response.status}")
                return
        except urllib.error.HTTPError as exc:
            if expect_route and exc.code == 404:
                _log(f"{url} answered HTTP 404, the route is not loaded yet")
                time.sleep(10)
                continue
            _log(f"{url} answered HTTP {exc.code}")
            return
        except OSError:
            time.sleep(10)
    raise TimeoutError(f"{url} did not answer within {timeout}s")


def _metapaas_password(core: Core, timeout: int = 300) -> str:
    """Read the metapaas IAM password the element generated in the core.

    It is a `$core.secret.passwords` resource in the metapaas system project;
    the value only appears once the secret has been reconciled.
    """
    _log(f"Reading the '{METAPAAS_PASSWORD_SECRET}' secret…")
    listing = ["secret", "passwords", "list", "-f", f"name={METAPAAS_PASSWORD_SECRET}"]
    # Scoped to the project that owns the secret first, unscoped as a fallback.
    attempts = [["-P", METAPAAS_SECRET_PROJECT_ID, *listing], listing]
    deadline = time.monotonic() + timeout
    while True:
        for args in attempts:
            for secret in core.json(args):
                value = str(secret.get("value", "") or "").strip()
                if value:
                    return value
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"The '{METAPAAS_PASSWORD_SECRET}' secret has no value after {timeout}s"
            )
        time.sleep(10)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Prepare Exordos Core for exordos_s3 functional testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--project-dir", default=".", help="Path to this repository")
    p.add_argument("--output-dir", required=True, help="Directory for build output")
    p.add_argument("--key-dir", default=None, help="Directory for the SSH key pair")
    p.add_argument(
        "-i",
        "--developer-key-path",
        default=None,
        help="Public key to inject into the images instead of a fresh one",
    )
    p.add_argument(
        "--http-host",
        default=None,
        help="Address the VMs reach the artifact server at (default: auto-detected)",
    )
    p.add_argument(
        "--http-port",
        type=int,
        default=8081,
        help="Port of the artifact server (default: 8081)",
    )
    p.add_argument(
        "--skip-build", action="store_true", help="Reuse the existing build output"
    )
    p.add_argument(
        "--skip-install",
        action="store_true",
        help="Only build and serve, do not touch the core",
    )
    p.add_argument(
        "--endpoint",
        default=os.environ.get("EXORDOS_ENDPOINT", "http://10.20.0.2/api/core"),
    )
    p.add_argument("--username", default=os.environ.get("EXORDOS_USERNAME", "admin"))
    p.add_argument("--password", default=os.environ.get("EXORDOS_PASSWORD", ""))
    p.add_argument(
        "--wait-timeout",
        type=int,
        default=600,
        help="Seconds to wait for nodes/elements to become ACTIVE",
    )
    p.add_argument(
        "--cleanup", action="store_true", help="Stop the artifact server and exit"
    )
    p.add_argument("--pid-file", default=None)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    output_dir = pathlib.Path(args.output_dir)
    key_dir = pathlib.Path(
        args.key_dir or pathlib.Path(tempfile.gettempdir()) / "exordos-test-keys"
    )
    pid_file = pathlib.Path(
        args.pid_file or pathlib.Path(tempfile.gettempdir()) / "s3aas-artifacts.pid"
    )

    if args.cleanup:
        _stop_http_server(pid_file)
        return

    base_url = f"http://{args.http_host or _default_ip()}:{args.http_port}"
    repository_url = f"{base_url}/{ELEMENTS_DIR}"
    index_url = f"{base_url}/{INDEX_DIR}/"
    index_dir = output_dir / INDEX_DIR / PACKAGE_INDEX_NAME
    # Staged outside the element output: `exordos build -f` rewrites that tree.
    wheel_stage = output_dir.parent / f"{output_dir.name}-wheel"

    _log("Step 1: SSH key pair")
    if args.developer_key_path:
        pub_key = pathlib.Path(args.developer_key_path)
    else:
        _, pub_key = _generate_ssh_key(key_dir)

    if args.skip_build:
        _log("Step 2: Skipping build (--skip-build)")
    else:
        _log("Step 2a: Building the exordos_s3 wheel")
        wheel = _build_wheel(args.project_dir, wheel_stage)

        _log("Step 2b: Building the elements")
        _build_element(
            args.project_dir,
            output_dir,
            pub_key,
            {
                # Where the CP downloads the dataplane image from.
                "repository": repository_url,
                # Where the metapaas plugin reconciler pip-installs the plugin
                # from, pinned to the wheel built above so the published PyPI
                # release cannot shadow it.
                "index_url": index_url,
                "package_version": _wheel_version(wheel),
            },
        )

        _log("Step 2c: Publishing the wheel into the pip index")
        index_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(wheel, index_dir / wheel.name)

    version = _element_version(output_dir)
    _log(f"Element {ELEMENT_NAME} version: {version}")

    _log("Step 3: Serving the build output")
    _start_http_server(output_dir, args.http_port, pid_file)
    _log(f"  element repository: {repository_url}/")
    _log(f"  pip index:          {index_url}")

    if args.skip_install:
        _log("Steps 4-6: Skipping install (--skip-install)")
        return

    core = Core(args.endpoint, args.username, args.password)

    _log("Step 4: Installing the metapaas element")
    _install_element(core, "metapaas")
    cp_ip = _wait_for_node(core, CP_NODE_NAME, args.wait_timeout)
    cp_url = f"http://{cp_ip}:{CP_API_PORT}"
    _wait_for_element(core, "metapaas", args.wait_timeout)
    _wait_for_api(f"{cp_url}/v1/", args.wait_timeout)

    _log("Step 5: Installing the s3aas element")
    _register_repository(core, f"{repository_url}/")
    # The core scans a freshly registered repository asynchronously, so the
    # element only becomes installable a moment later.
    _install_element(core, ELEMENT_NAME, version, retries=30)
    _wait_for_element(core, ELEMENT_NAME, args.wait_timeout)
    # The plugin reconciler pip-installs the plugin and reloads the user-api;
    # the s3 routes answer 404 until that is through.
    _wait_for_api(
        f"{cp_url}/v1/types/s3/versions/", args.wait_timeout, expect_route=True
    )

    _log("Step 6: Reading the metapaas IAM password")
    metapaas_password = _metapaas_password(core)

    _print_summary(args, cp_url, metapaas_password)


def _print_summary(
    args: argparse.Namespace, cp_url: str, metapaas_password: str
) -> None:
    _log("=" * 60)
    _log("Environment ready! Env vars for the functional tests:")
    _log("")
    # Printed without the [prepare-env] prefix so CI can grep these lines.
    print(f"  export EXORDOS_ENDPOINT={args.endpoint}", flush=True)
    print(f"  export EXORDOS_USERNAME={args.username}", flush=True)
    print(f"  export EXORDOS_PASSWORD={args.password}", flush=True)
    print(f"  export METAPAAS_USERNAME={METAPAAS_IAM_USER}", flush=True)
    print(f"  export METAPAAS_PASSWORD={metapaas_password}", flush=True)
    print(f"  export EXORDOS_S3_CP_URL={cp_url}", flush=True)
    print(f"  export EXORDOS_POLL_TIMEOUT={args.wait_timeout}", flush=True)
    _log("")
    _log("Then run:  tox -e py312-functional")
    _log("=" * 60)


if __name__ == "__main__":
    main()
