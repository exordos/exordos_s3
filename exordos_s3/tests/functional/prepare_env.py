#!/usr/bin/env python3
"""Prepare an Exordos Core environment for metapaas_s3 integration testing.

Steps:
  1. Generate SSH key pair.
  2. Build metapaas_s3 DP image + wheel (from --project-dir).
     Optionally build exordos_metapaas CP image (from --metapaas-dir).
  3. Serve s3 artifacts via a local HTTP server.
  4. Install metapaas element (from official repo, or local if --metapaas-dir given);
     wait for CP node ACTIVE.
  5. Install s3aas element; wait for PluginReconciler to activate s3 plugin.
  6. Print env vars needed by the functional test suite.

Usage::

    python prepare_env.py \\
        --project-dir . \\
        --output-dir /tmp/metapaas-s3-build \\
        --endpoint http://10.20.0.2/api/core \\
        --username admin --password <pass>
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOCAL_REPO_PATH = "/srv/exordos-local-repo/exordos-elements"
METAPAAS_PROJECT_ID = "4d657461-0000-0000-0000-000000000002"
METAPAAS_IAM_USER = "metapaas"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    print(f"[prepare-env] {msg}", flush=True)


def _get_default_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    _log(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, **kwargs)


def _generate_ssh_key(key_dir: pathlib.Path) -> tuple[str, str]:
    key_dir.mkdir(parents=True, exist_ok=True)
    priv = key_dir / "id_rsa"
    pub = key_dir / "id_rsa.pub"
    if pub.exists():
        _log(f"SSH public key already exists: {pub}")
        return str(priv), str(pub)
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
    _log(f"Generated SSH key pair in {key_dir}")
    return str(priv), str(pub)


def _build(
    project_dir: str, output_dir: str, pub_key: str, manifest_vars: dict
) -> None:
    cmd = [
        "exordos",
        "build",
        "-i",
        pub_key,
        "-f",
        "--output-dir",
        output_dir,
        project_dir,
    ]
    for k, v in manifest_vars.items():
        cmd += ["--manifest-var", f"{k}={v}"]
    _run(cmd)


def _build_wheel(project_dir: str, output_dir: str) -> pathlib.Path:
    """Build Python wheel for exordos_s3."""
    dist_dir = pathlib.Path(output_dir) / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    _run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist_dir)],
        cwd=project_dir,
    )
    wheels = list(dist_dir.glob("exordos_s3-*.whl"))
    if not wheels:
        raise FileNotFoundError(f"No wheel found in {dist_dir}")
    _log(f"Built wheel: {wheels[0].name}")
    return wheels[0]


def _start_http_server(serve_dir: str, port: int) -> subprocess.Popen:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", port))
        except OSError:
            raise RuntimeError(f"Port {port} already in use")

    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--directory", serve_dir],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    time.sleep(1)
    if proc.poll() is not None:
        raise RuntimeError(f"HTTP server failed to start on port {port}")
    _log(f"HTTP server: port={port} dir={serve_dir}")
    return proc


def _stop_http_server(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass


def _publish_to_serve_dir(
    serve_root: pathlib.Path,
    metapaas_output: pathlib.Path | None,
    s3aas_output: pathlib.Path,
    wheel_path: pathlib.Path,
) -> None:
    """Arrange build artifacts into the directory structure the manifests expect.

    metapaas CP image:  serve_root/metapaas/<version>/images/exordos-metapaas.raw.zst (optional)
    s3aas DP image:     serve_root/s3aas/<version>/images/exordos-metapaas-s3-dp.raw.zst
    s3aas manifest:     serve_root/s3aas/<version>/s3aas.yaml
    pip wheel:          serve_root/simple/exordos_s3-*.whl
    """

    def _read_version(output_dir: pathlib.Path) -> str:
        inv = output_dir / "inventory.json"
        if inv.exists():
            data = json.loads(inv.read_text())
            if isinstance(data, list):
                data = data[0]
            return data.get("version", "0.0.1")
        return "0.0.1"

    if metapaas_output is not None:
        # metapaas CP image (only when built locally)
        mp_ver = _read_version(metapaas_output)
        mp_img_dir = serve_root / "metapaas" / mp_ver / "images"
        mp_img_dir.mkdir(parents=True, exist_ok=True)
        for img in (metapaas_output / "images").glob("*.zst"):
            dst = mp_img_dir / img.name
            if not dst.exists():
                shutil.copy2(img, dst)
            _log(f"  metapaas image: metapaas/{mp_ver}/images/{img.name}")

    # s3aas DP image + manifest
    s3_ver = _read_version(s3aas_output)
    s3_img_dir = serve_root / "s3aas" / s3_ver / "images"
    s3_img_dir.mkdir(parents=True, exist_ok=True)
    for img in (s3aas_output / "images").glob("*.zst"):
        dst = s3_img_dir / img.name
        if not dst.exists():
            shutil.copy2(img, dst)
        _log(f"  s3aas DP image: s3aas/{s3_ver}/images/{img.name}")
    for mf in (s3aas_output / "manifests").glob("*.yaml"):
        dst = serve_root / "s3aas" / s3_ver / mf.name
        shutil.copy2(mf, dst)
        _log(f"  s3aas manifest: s3aas/{s3_ver}/{mf.name}")

    # pip wheel
    pip_dir = serve_root / "simple"
    pip_dir.mkdir(parents=True, exist_ok=True)
    dst = pip_dir / wheel_path.name
    if not dst.exists():
        shutil.copy2(wheel_path, dst)
    _log(f"  pip wheel: simple/{wheel_path.name}")


def _ee_install(
    name: str,
    version: str,
    repository: str | None,
    endpoint: str,
    username: str,
    password: str,
) -> None:
    cmd = [
        "exordos",
        "-e",
        endpoint,
        "-u",
        username,
        "-p",
        password,
        "ee",
        "install",
        name,
        "--version",
        version,
    ]
    if repository is not None:
        cmd += ["--repository", repository]
    _run(cmd)


def _wait_for_element(
    name: str,
    target: str,
    endpoint: str,
    username: str,
    password: str,
    timeout: int = 300,
) -> None:
    _log(f"Waiting for element '{name}' to reach {target}…")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["exordos", "-e", endpoint, "-u", username, "-p", password, "ee", "list"],
            capture_output=True,
            text=True,
        )
        for line in result.stdout.splitlines():
            if name in line:
                if target in line:
                    _log(f"Element '{name}' is {target}")
                    return
                if "ERROR" in line:
                    raise RuntimeError(f"Element '{name}' entered ERROR state")
        time.sleep(15)
    raise TimeoutError(f"Element '{name}' did not reach {target} within {timeout}s")


def _wait_for_node(
    name_pattern: str, endpoint: str, username: str, password: str, timeout: int = 300
) -> str:
    """Wait for a compute node matching name_pattern to be ACTIVE; return its IP."""
    _log(f"Waiting for node matching '{name_pattern}' to be ACTIVE…")
    deadline = time.monotonic() + timeout
    last_raw = ""
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["exordos", "-e", endpoint, "-u", username, "-p", password, "cn", "list", "-o", "json"],
            capture_output=True,
            text=True,
        )
        last_raw = result.stdout
        try:
            nodes = json.loads(result.stdout)
        except Exception:
            time.sleep(15)
            continue
        for node in nodes if isinstance(nodes, list) else []:
            name = str(node.get("name", ""))
            status = str(node.get("status", ""))
            if name_pattern in name and status == "ACTIVE":
                for val in node.values():
                    m = re.search(r"\b(10\.\d+\.\d+\.\d+)\b", str(val))
                    if m:
                        ip = m.group(1)
                        _log(f"Node '{name}' ACTIVE at {ip}")
                        return ip
        time.sleep(15)
    _log(f"Last cn list output:\n{last_raw}")
    raise TimeoutError(f"No ACTIVE node matching '{name_pattern}' within {timeout}s")


def _get_metapaas_iam_password(cp_ip: str) -> str:
    """Read IAM_USER_PASS from /etc/exordos_init.txt on the metapaas CP."""
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "ConnectTimeout=10",
                f"root@{cp_ip}",
                "grep IAM_USER_PASS /etc/exordos_init.txt | cut -d= -f2",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        pw = result.stdout.strip()
        if pw:
            return pw
    except Exception as e:
        _log(f"WARNING: Could not read IAM password via SSH: {e}")

    # Fallback: read via virsh guest-exec (if running on the hypervisor host)
    try:
        # Find VM name by IP from virsh
        virsh_result = subprocess.run(
            ["sudo", "virsh", "list", "--all"],
            capture_output=True,
            text=True,
        )
        for line in virsh_result.stdout.splitlines():
            if "metapaas-cp" in line:
                vm_name = line.split()[1]
                script = "cat /etc/exordos_init.txt | grep IAM_USER_PASS | cut -d= -f2"
                enc = subprocess.run(
                    ["base64", "-w0"], input=script.encode(), capture_output=True
                ).stdout.decode()
                pid_result = subprocess.run(
                    [
                        "sudo",
                        "virsh",
                        "qemu-agent-command",
                        vm_name,
                        f'{{"execute":"guest-exec","arguments":{{"path":"/bin/bash","arg":["-c","echo {enc} | base64 -d | bash"],"capture-output":true}}}}',
                    ],
                    capture_output=True,
                    text=True,
                )
                pid = json.loads(pid_result.stdout)["return"]["pid"]
                time.sleep(2)
                status = subprocess.run(
                    [
                        "sudo",
                        "virsh",
                        "qemu-agent-command",
                        vm_name,
                        f'{{"execute":"guest-exec-status","arguments":{{"pid":{pid}}}}}',
                    ],
                    capture_output=True,
                    text=True,
                )
                import base64

                out = json.loads(status.stdout)["return"]
                pw = base64.b64decode(out.get("out-data", "")).decode().strip()
                if pw:
                    return pw
    except Exception as e:
        _log(f"WARNING: Could not read IAM password via virsh: {e}")

    return METAPAAS_IAM_USER  # fallback to default


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Prepare Exordos Core for metapaas_s3 integration testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--metapaas-dir",
        default=None,
        help="Path to exordos_metapaas source. If omitted, installs metapaas from the official repo.",
    )
    p.add_argument(
        "--project-dir", default=".", help="Path to metapaas_s3 repository (default: .)"
    )
    p.add_argument("--output-dir", required=True, help="Directory for build output")
    p.add_argument("--key-dir", default=None, help="Directory for SSH key pair")
    p.add_argument(
        "-i", "--developer-key-path", default=None, help="Path to developer public key"
    )
    p.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip exordos build (use existing output)",
    )
    p.add_argument(
        "--http-port",
        type=int,
        default=8000,
        help="Port for the image HTTP server (default: 8000)",
    )
    p.add_argument(
        "--http-host",
        default=None,
        help="Host/IP for repository URL (default: auto-detect)",
    )
    p.add_argument(
        "--no-http-server",
        action="store_true",
        help="Do not start HTTP server (images served elsewhere)",
    )
    p.add_argument(
        "--metapaas-version",
        default="latest",
        help="metapaas element version to install",
    )
    p.add_argument(
        "--s3aas-version", default="0.0.1", help="s3aas element version to install"
    )
    p.add_argument(
        "--skip-install", action="store_true", help="Skip element installation"
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
        help="Seconds to wait for elements/nodes to become ACTIVE",
    )
    p.add_argument(
        "--cleanup", action="store_true", help="Stop the HTTP server and exit"
    )
    p.add_argument(
        "--repository",
        default=None,
        help="Element repository base URL (overrides the auto-detected HTTP server URL).",
    )
    p.add_argument(
        "--index-url",
        dest="index_url",
        default=None,
        help="pip index URL (overrides the auto-detected HTTP server URL).",
    )
    p.add_argument("--pid-file", default=None)
    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    output_dir = pathlib.Path(args.output_dir)
    key_dir = (
        pathlib.Path(args.key_dir)
        if args.key_dir
        else pathlib.Path(tempfile.gettempdir()) / "exordos-test-keys"
    )
    pid_file = (
        pathlib.Path(args.pid_file)
        if args.pid_file
        else pathlib.Path(tempfile.gettempdir()) / "metapaas-http-server.pid"
    )

    if args.cleanup:
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            try:
                os.kill(pid, signal.SIGTERM)
                _log(f"Stopped HTTP server (PID {pid})")
            except ProcessLookupError:
                _log(f"HTTP server PID {pid} not running")
            pid_file.unlink(missing_ok=True)
        return

    # HTTP server base URL
    http_proc = None
    repository_url = None
    index_url = None

    if not args.no_http_server:
        host = args.http_host or _get_default_ip()
        port = args.http_port
        repository_url = f"http://{host}:{port}"
        index_url = f"http://{host}:{port}/simple/"

    # Explicit CLI overrides always win (used when nginx is managed externally).
    if args.repository is not None:
        repository_url = args.repository
    if args.index_url is not None:
        index_url = args.index_url

    metapaas_output = output_dir / "metapaas"
    s3aas_output = output_dir / "s3aas"
    serve_root = output_dir / "serve"
    wheel_output = output_dir / "wheel"

    # ------------------------------------------------------------------
    # Step 1: SSH key
    # ------------------------------------------------------------------
    _log("Step 1: SSH key pair")
    _, pub_key = _generate_ssh_key(key_dir)
    pub_key = args.developer_key_path or pub_key

    # ------------------------------------------------------------------
    # Step 2: Build exordos_metapaas
    # ------------------------------------------------------------------
    if not args.skip_build:
        if args.metapaas_dir is not None:
            _log("Step 2a: Building exordos_metapaas")
            mp_vars: dict[str, str] = {}
            if repository_url:
                mp_vars["repository"] = repository_url
            _build(args.metapaas_dir, str(metapaas_output), pub_key, mp_vars)
        else:
            _log(
                "Step 2a: Skipping exordos_metapaas build (will install from official repo)"
            )

        _log("Step 2b: Building metapaas_s3 (DP image + manifests)")
        s3_vars: dict[str, str] = {}
        if repository_url:
            s3_vars["repository"] = repository_url
        if index_url:
            s3_vars["index_url"] = index_url
        _build(args.project_dir, str(s3aas_output), pub_key, s3_vars)

        _log("Step 2c: Building Python wheel for exordos_s3")
        wheel_path = _build_wheel(args.project_dir, str(wheel_output))
    else:
        _log("Step 2: Skipping build (--skip-build)")
        wheel_path = None

    # ------------------------------------------------------------------
    # Step 3: Publish to serve directory + start HTTP server
    # ------------------------------------------------------------------
    if not args.no_http_server:
        if wheel_path is None:
            # skip-build mode: locate a previously built wheel
            wheels = list((wheel_output / "dist").glob("exordos_s3-*.whl"))
            if not wheels:
                raise FileNotFoundError(
                    "No wheel found; run without --skip-build first"
                )
            wheel_path = wheels[0]

        _log("Step 3: Publishing artifacts")
        serve_root.mkdir(parents=True, exist_ok=True)
        _publish_to_serve_dir(
            serve_root,
            metapaas_output if args.metapaas_dir is not None else None,
            s3aas_output,
            wheel_path,
        )
        http_proc = _start_http_server(str(serve_root), port)
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(http_proc.pid))
        _log(f"Repository URL: {repository_url}")
        _log(f"Index URL:      {index_url}")
    else:
        _log("Step 3: Skipping local HTTP server (--no-http-server)")

    if args.skip_install:
        _log("Step 4-6: Skipping install (--skip-install)")
        _print_summary(repository_url, index_url, args, "?", "?")
        return

    # ------------------------------------------------------------------
    # Step 4: Install metapaas element
    # ------------------------------------------------------------------
    _log("Step 4: Installing metapaas element")
    _ee_install(
        "metapaas",
        args.metapaas_version,
        repository_url if args.metapaas_dir is not None else None,
        args.endpoint,
        args.username,
        args.password,
    )

    _log("Step 4a: Waiting for metapaas CP node ACTIVE")
    cp_ip = _wait_for_node(
        "metapaas-cp",
        args.endpoint,
        args.username,
        args.password,
        timeout=args.wait_timeout,
    )

    # ------------------------------------------------------------------
    # Step 5: Install s3aas element (triggers PluginReconciler)
    # ------------------------------------------------------------------
    _log("Step 5: Installing s3aas element")
    _ee_install(
        "s3aas",
        args.s3aas_version,
        repository_url,
        args.endpoint,
        args.username,
        args.password,
    )

    _log("Step 5a: Waiting for s3aas element ACTIVE (PluginReconciler installs plugin)")
    _wait_for_element(
        "s3aas",
        "ACTIVE",
        args.endpoint,
        args.username,
        args.password,
        timeout=args.wait_timeout,
    )

    # ------------------------------------------------------------------
    # Step 6: Get metapaas IAM password
    # ------------------------------------------------------------------
    _log("Step 6: Reading metapaas IAM password")
    metapaas_password = _get_metapaas_iam_password(cp_ip)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    _print_summary(repository_url, index_url, args, cp_ip, metapaas_password)


def _print_summary(repository_url, index_url, args, cp_ip, metapaas_password) -> None:
    _log("=" * 60)
    _log("Environment ready! Suggested env vars for functional tests:")
    _log("")
    # Print without the [prepare-env] prefix so these lines are grep-able by CI.
    print(f"  export EXORDOS_ENDPOINT={args.endpoint}", flush=True)
    print(f"  export EXORDOS_USERNAME={args.username}", flush=True)
    print(f"  export EXORDOS_PASSWORD={args.password}", flush=True)
    print(f"  export METAPAAS_USERNAME={METAPAAS_IAM_USER}", flush=True)
    print(f"  export METAPAAS_PASSWORD={metapaas_password}", flush=True)
    print(f"  export EXORDOS_S3_CP_URL=http://{cp_ip}:8080", flush=True)
    print("  export EXORDOS_POLL_TIMEOUT=600", flush=True)
    _log("")
    _log("Then run:  tox -e py312-functional")
    _log("=" * 60)


if __name__ == "__main__":
    main()
