#!/usr/bin/env python3
#    Copyright 2025 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""Prepare a Exordos Core environment for integration testing.

Steps:
1. Generate an SSH key pair (inject into VM images).
2. Build element images via ``exordos build``.
3. Serve images via a local HTTP server.
4. Install the element manifest into the running Exordos Core.

This script is generic and can be used for any PaaS element, not only S3aaS.

Usage examples::

    # Prepare S3aaS test environment
    python prepare_env.py \\
        --project-dir ./exordos \\
        --output-dir /tmp/s3aas-build \\
        --manifest-path /tmp/s3aas-build/manifests/s3aas.yaml \\
        --manifest-var repository=http://10.20.0.1:8000

    # Prepare DBaaS test environment
    python prepare_env.py \\
        --project-dir ../exordos_db/exordos \\
        --output-dir /tmp/dbaas-build \\
        --manifest-path /tmp/dbaas-build/manifests/dbaas.yaml \\
        --manifest-var repository=http://10.20.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import tempfile
import time

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    print(f"[prepare-env] {msg}", flush=True)


def _get_default_ip() -> str:
    """Return the IP of the default route interface."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _run(
    cmd: list[str], env: dict | None = None, **kwargs
) -> subprocess.CompletedProcess:
    _log(f"  $ {' '.join(cmd)}")
    run_env = None
    if env:
        run_env = {**os.environ, **env}
    return subprocess.run(cmd, check=True, env=run_env, **kwargs)


def _generate_ssh_key(key_dir: str) -> tuple[str, str]:
    """Generate an SSH key pair. Return (private_key_path, public_key_path)."""
    key_dir = pathlib.Path(key_dir)
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
            "exordos-test-env",
        ]
    )
    _log(f"Generated SSH key pair in {key_dir}")
    return str(priv), str(pub)


def _build_element(
    project_dir: str,
    output_dir: str,
    public_key_path: str,
    force: bool = False,
    manifest_vars: dict[str, str] | None = None,
) -> None:
    """Run ``exordos build`` to produce images and rendered manifests."""
    cmd = [
        "exordos",
        "build",
        "-i",
        public_key_path,
        "-f",
        "--output-dir",
        output_dir,
        project_dir,
    ]
    if force:
        cmd.append("--force")

    if manifest_vars:
        for k, v in manifest_vars.items():
            cmd.extend(["--manifest-var", f"{k}={v}"])

    _run(cmd)
    _log(f"Element built, output in {output_dir}")


def _start_http_server(
    serve_dir: str,
    port: int,
) -> subprocess.Popen:
    """Start a background HTTP server serving *serve_dir* on *port*."""
    # Verify the port is available
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", port))
        except OSError:
            raise RuntimeError(f"Port {port} is already in use")

    cmd = [
        sys.executable,
        "-m",
        "http.server",
        str(port),
        "--directory",
        serve_dir,
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    # Give the server a moment to bind
    time.sleep(1)
    if proc.poll() is not None:
        raise RuntimeError(f"HTTP server failed to start on port {port}")
    _log(f"HTTP server started on port {port}, serving {serve_dir}")
    return proc


def _stop_http_server(proc: subprocess.Popen) -> None:
    """Stop the background HTTP server."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    _log("HTTP server stopped")


def _create_image_symlinks(output_dir: pathlib.Path) -> None:
    """Create symlinks so the HTTP server serves images at the URL the
    manifest expects.

    The manifest references images as:
        {repository}/{element_name}/{version}/images/{image_file}

    New exordos build layout puts them at:
        {output_dir}/exordos-elements/{name}/{version}/images/{image_file}
    A single element-level symlink bridges the gap:
        {output_dir}/{name} -> exordos-elements/{name}

    Legacy exordos build layout puts them at:
        {output_dir}/images/{image_file}
    Per-file symlinks are created under {output_dir}/{name}/{version}/images/.
    """
    elements_dir = output_dir / "exordos-elements"
    new_inventory_path = elements_dir / "inventory.json"
    old_inventory_path = output_dir / "inventory.json"

    if new_inventory_path.exists():
        with open(new_inventory_path) as f:
            data = json.load(f)
        elements = data.get("elements", {})
        created = 0
        for name, versions in elements.items():
            for info in versions.values():
                if not info.get("images"):
                    continue
                link = output_dir / name
                src = pathlib.Path("exordos-elements") / name
                if not link.exists():
                    link.symlink_to(src)
                    _log(f"  Symlink: {link} -> {src}")
                    created += 1
        if created:
            _log("Image symlinks created")
        return

    if not old_inventory_path.exists():
        _log("WARNING: No inventory.json found, skipping symlink creation")
        return

    with open(old_inventory_path) as f:
        inventories = json.load(f)

    if not isinstance(inventories, list):
        inventories = [inventories]

    inv = inventories[0]
    name = inv["name"]
    version = inv["version"]

    # Target: {output_dir}/{name}/{version}/images/
    target_dir = output_dir / name / version / "images"
    target_dir.mkdir(parents=True, exist_ok=True)

    for img_path in inv.get("images", []):
        img_name = pathlib.Path(img_path).name
        link = target_dir / img_name
        src = pathlib.Path("../../../images") / img_name
        if not link.exists():
            link.symlink_to(src)
            _log(f"  Symlink: {link} -> {src}")

    _log("Image symlinks created")


def _get_primary_manifest(output_dir: pathlib.Path) -> str:
    """Return the manifest path for the primary element from inventory.json.

    Supports both the new layout ({output_dir}/exordos-elements/inventory.json)
    and the legacy layout ({output_dir}/inventory.json).  The primary element
    is the first one that ships images; if none do, the first element is used.
    """
    elements_dir = output_dir / "exordos-elements"
    new_inventory_path = elements_dir / "inventory.json"

    if new_inventory_path.exists():
        with open(new_inventory_path) as f:
            data = json.load(f)
        elements = data.get("elements", {})
        if not elements:
            raise FileNotFoundError(
                f"No elements listed in {new_inventory_path}"
            )
        # Prefer the first element that has images (the deployable one).
        primary: dict | None = None
        for versions in elements.values():
            for info in versions.values():
                if primary is None or info.get("images"):
                    primary = info
                if primary.get("images"):
                    break
            if primary and primary.get("images"):
                break

        name = primary["name"]
        version = primary["version"]
        manifests = primary.get("manifests", [])
        if not manifests:
            raise FileNotFoundError(
                f"No manifests listed for element '{name}' in {new_inventory_path}"
            )
        return str(elements_dir / name / version / "manifests" / manifests[0])

    inventory_path = output_dir / "inventory.json"
    if not inventory_path.exists():
        raise FileNotFoundError(f"No inventory.json found at {inventory_path}")

    with open(inventory_path) as f:
        inventories = json.load(f)

    if not isinstance(inventories, list):
        inventories = [inventories]

    primary = inventories[0]
    manifests = primary.get("manifests", [])
    if not manifests:
        raise FileNotFoundError(
            f"No manifests listed for element '{primary['name']}' in inventory.json"
        )
    return manifests[0]


def _install_element(
    manifest_path: str,
    endpoint: str,
    username: str,
    password: str,
    project_id: str | None = None,
    repository: str | None = None,
    no_proxy: str = "",
) -> None:
    """Install or update the element manifest in Exordos Core via CLI."""
    base_cmd = [
        "exordos",
        "-e",
        endpoint,
        "-u",
        username,
        "-p",
        password,
    ]
    if project_id:
        base_cmd.extend(["-P", project_id])

    proxy_env: dict[str, str] = {}
    if no_proxy:
        proxy_env["NO_PROXY"] = no_proxy
        proxy_env["no_proxy"] = no_proxy

    # Try install first; if element already exists, use update instead.
    install_cmd = base_cmd + ["e", "elements", "install", manifest_path]
    if repository:
        install_cmd.extend(["-r", repository])

    run_env = {**os.environ, **proxy_env} if proxy_env else None
    result = subprocess.run(install_cmd, capture_output=True, text=True, env=run_env)
    if result.returncode == 0:
        _log(f"Element installed from {manifest_path}")
        return

    # Check if the failure is "already installed"
    if "already installed" in (result.stderr + result.stdout).lower():
        _log("Element already installed, switching to update")
        update_cmd = base_cmd + ["e", "elements", "update", manifest_path]
        if repository:
            update_cmd.extend(["-r", repository])
        _run(update_cmd, env=proxy_env or None)
        _log(f"Element updated from {manifest_path}")
    else:
        # Re-raise with original error
        _log(f"ERROR: {result.stderr or result.stdout}")
        raise subprocess.CalledProcessError(
            result.returncode, install_cmd, result.stdout, result.stderr
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Prepare a Exordos Core environment for integration testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Build options
    p.add_argument(
        "--project-dir",
        required=True,
        help="Path to the exordos project directory (contains exordos.yaml)",
    )
    p.add_argument(
        "--output-dir",
        required=True,
        help="Directory for build output (images, manifests, inventory)",
    )
    p.add_argument(
        "--key-dir",
        default=None,
        help="Directory for SSH key pair (default: /tmp/exordos-test-keys)",
    )
    p.add_argument(
        "-i",
        "--developer-key-path",
        default=None,
        help="Path to developer public key (forwarded to exordos build -i)",
    )
    p.add_argument(
        "--force-build",
        action="store_true",
        help="Force rebuild even if output already exists",
    )
    p.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip exordos build (images already built)",
    )

    # Manifest vars (forwarded to exordos build --manifest-var)
    p.add_argument(
        "--manifest-var",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Additional manifest variable (can be repeated)",
    )

    # HTTP server
    p.add_argument(
        "--http-port",
        type=int,
        default=8000,
        help="Port for the image HTTP server",
    )
    p.add_argument(
        "--http-host",
        default=None,
        help="Host/IP for repository URL (default: auto-detect)",
    )
    p.add_argument(
        "--no-http-server",
        action="store_true",
        help="Do not start an HTTP server (images served elsewhere)",
    )

    # Install options
    p.add_argument(
        "--manifest-path",
        default=None,
        help=(
            "Path to the rendered manifest YAML to install. "
            "Default: first manifest from inventory.json"
        ),
    )
    p.add_argument(
        "--endpoint",
        default=os.environ.get("EXORDOS_ENDPOINT", "http://10.20.0.2:11010"),
        help="Exordos Core API endpoint",
    )
    p.add_argument(
        "--username",
        default=os.environ.get("EXORDOS_USERNAME", "admin"),
        help="Exordos Core admin username",
    )
    p.add_argument(
        "--password",
        default=os.environ.get("EXORDOS_PASSWORD", ""),
        help="Exordos Core admin password",
    )
    p.add_argument(
        "--project-id",
        default=os.environ.get("EXORDOS_PROJECT_ID"),
        help="Exordos Core project ID for scoped auth",
    )
    p.add_argument(
        "--repository",
        default=None,
        help="Element repository URL for dependency resolution",
    )
    p.add_argument(
        "--skip-install",
        action="store_true",
        help="Build and serve only, skip installing the manifest",
    )
    p.add_argument(
        "--no-proxy",
        default=os.environ.get(
            "NO_PROXY",
            "10.20.0.0/22,localhost,127.0.0.1,repo.exordos.com",
        ),
        help="NO_PROXY value for exordos CLI calls",
    )

    # Cleanup
    p.add_argument(
        "--cleanup",
        action="store_true",
        help="Stop the HTTP server and exit (use after Ctrl-C)",
    )
    p.add_argument(
        "--pid-file",
        default=None,
        help="File to store the HTTP server PID for later cleanup",
    )

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    output_dir = pathlib.Path(args.output_dir)
    # Key dir must be OUTSIDE output_dir because exordos build --force
    # does shutil.rmtree(output_dir), destroying everything inside.
    key_dir = (
        pathlib.Path(args.key_dir)
        if args.key_dir
        else pathlib.Path(tempfile.gettempdir()) / "exordos-test-keys"
    )

    # -- Cleanup mode: just stop the HTTP server
    if args.cleanup:
        pid_file = (
            pathlib.Path(args.pid_file)
            if args.pid_file
            else pathlib.Path(tempfile.gettempdir()) / "exordos-http-server.pid"
        )
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            try:
                os.kill(pid, signal.SIGTERM)
                _log(f"Stopped HTTP server (PID {pid})")
            except ProcessLookupError:
                _log(f"HTTP server PID {pid} not running")
            pid_file.unlink(missing_ok=True)
        else:
            _log("No PID file found, nothing to clean up")
        return

    # Parse manifest vars
    manifest_vars: dict[str, str] = {}
    for mv in args.manifest_var:
        if "=" not in mv:
            parser.error(f"Invalid --manifest-var: '{mv}', expected KEY=VALUE")
        k, v = mv.split("=", 1)
        manifest_vars[k] = v

    # ------------------------------------------------------------------
    # Pre-compute HTTP server port/host so repository URL is consistent
    # with the manifest rendered during build.
    # ------------------------------------------------------------------
    http_proc = None
    repository_url = None

    if not args.no_http_server:
        port = args.http_port
        host = args.http_host or _get_default_ip()
        repository_url = f"http://{host}:{port}"

        # If the user didn't pass a 'repository' manifest var, inject it
        # automatically so the rendered manifest points to our HTTP server.
        if "repository" not in manifest_vars:
            manifest_vars["repository"] = repository_url
            _log(f"Auto-injected manifest var: repository={repository_url}")

    # ------------------------------------------------------------------
    # Step 1: Generate SSH key pair
    # ------------------------------------------------------------------
    _log("Step 1: Generating SSH key pair")
    priv_key, pub_key = _generate_ssh_key(str(key_dir))

    # ------------------------------------------------------------------
    # Step 2: Build element images
    # ------------------------------------------------------------------
    if args.skip_build:
        _log("Step 2: Skipping build (--skip-build)")
        if not output_dir.exists():
            _log(f"ERROR: Output dir {output_dir} does not exist, run build first")
            sys.exit(1)
    else:
        _log("Step 2: Building element images")
        _build_element(
            project_dir=args.project_dir,
            output_dir=str(output_dir),
            public_key_path=args.developer_key_path or pub_key,
            force=args.force_build,
            manifest_vars=manifest_vars,
        )

    # ------------------------------------------------------------------
    # Step 3: Start HTTP server for image distribution
    # ------------------------------------------------------------------
    if not args.no_http_server:
        _log("Step 3: Starting HTTP server for image distribution")

        # The manifest references images as:
        #   {repository}/s3aas/{version}/images/exordos-s3.raw.zst
        # But exordos build puts them at:
        #   {output_dir}/images/exordos-s3.raw.zst
        # Create a symlink tree so the HTTP server can serve the
        # expected URL structure from the output directory root.
        _create_image_symlinks(output_dir)

        http_proc = _start_http_server(str(output_dir), port)

        # Save PID for cleanup
        pid_file = (
            pathlib.Path(args.pid_file)
            if args.pid_file
            else pathlib.Path(tempfile.gettempdir()) / "exordos-http-server.pid"
        )
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(http_proc.pid))
        _log(f"HTTP server PID saved to {pid_file}")
        _log(f"Repository URL: {repository_url}")
    else:
        _log("Step 3: Skipping HTTP server (--no-http-server)")

    # ------------------------------------------------------------------
    # Step 4: Install element manifest into Exordos Core
    # ------------------------------------------------------------------
    if not args.skip_install:
        _log("Step 4: Installing element manifest into Exordos Core")

        manifest_path = args.manifest_path
        if not manifest_path:
            manifest_path = _get_primary_manifest(output_dir)
            _log(f"Auto-detected manifest: {manifest_path}")

        _install_element(
            manifest_path=manifest_path,
            endpoint=args.endpoint,
            username=args.username,
            password=args.password,
            project_id=args.project_id,
            repository=args.repository,
            no_proxy=args.no_proxy,
        )
    else:
        _log("Step 4: Skipping install (--skip-install)")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    _log("=" * 60)
    _log("Environment prepared successfully!")
    _log(f"  Output dir:     {output_dir}")
    _log(f"  SSH key:        {pub_key}")
    if repository_url:
        _log(f"  Repository URL: {repository_url}")
    if not args.skip_install:
        _log(f"  Element installed at: {args.endpoint}")
    _log("")
    _log("To stop the HTTP server later:")
    _log(f"  python {__file__} --cleanup --output-dir {output_dir}")
    if http_proc:
        _log(f"  (or: kill {http_proc.pid})")
    _log("=" * 60)

    # Print env vars for test consumption
    _log("\nSuggested environment variables for tests:")
    if repository_url:
        _log("  EXORDOS_S3_CP_URL=http://s3aas-cp.local.genesis-core.tech:8080")
    _log(f"  EXORDOS_ENDPOINT={args.endpoint}")
    _log(f"  EXORDOS_USERNAME={args.username}")
    _log(f"  EXORDOS_PASSWORD={args.password}")
    if args.project_id:
        _log(f"  EXORDOS_PROJECT_ID={args.project_id}")


if __name__ == "__main__":
    main()
