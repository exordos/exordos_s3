"""Destructive regression: run as root on an explicitly selected disposable node.

EXORDOS_TEST_DISPOSABLE_S3_NODE=1 python -m pytest -v test_memory_pressure.py
Requires a ready, single-node instance, cgroup v2, pytest and minio. Never run
this test on a shared or production node. No credentials leave the guest.
"""

import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from minio import Minio
import pytest
import urllib3


UNIT = "exordos-metapaas-rustfs.service"
ESSENTIAL_UNITS = (
    "systemd-networkd.service",
    "ssh.service",
    "qemu-guest-agent.service",
    "exordos-universal-agent.service",
    "exordos-metapaas-s3-agent.service",
)
pytestmark = pytest.mark.skipif(
    os.environ.get("EXORDOS_TEST_DISPOSABLE_S3_NODE") != "1",
    reason="requires explicit opt-in on a disposable S3 node",
)


def properties(unit, *names):
    result = subprocess.run(
        ["systemctl", "show", unit, *[f"--property={name}" for name in names]],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines())


def essential_processes():
    pids = {}
    for unit in ESSENTIAL_UNITS:
        state = properties(unit, "ActiveState", "MainPID")
        assert state["ActiveState"] == "active", unit
        assert int(state["MainPID"]) > 0, unit
        pids[unit] = state["MainPID"]
    return pids


def events(cgroup):
    return dict(
        (key, int(value))
        for key, value in (
            line.split() for line in (cgroup / "memory.events").read_text().splitlines()
        )
    )


def test_oom_is_contained_and_objects_survive():
    assert os.geteuid() == 0, "run as root inside the disposable guest"
    state = properties(
        UNIT, "MainPID", "ControlGroup", "MemoryHigh", "MemoryMax", "OOMPolicy"
    )
    pid = int(state["MainPID"])
    assert pid > 0
    cgroup = Path("/sys/fs/cgroup") / state["ControlGroup"].lstrip("/")
    assert (cgroup / "memory.events").is_file(), "requires cgroup v2"
    ram = int(Path("/proc/meminfo").read_text().splitlines()[0].split()[1]) * 1024
    page = os.sysconf("SC_PAGE_SIZE")
    for property_name, percent in (("MemoryHigh", 70), ("MemoryMax", 75)):
        assert state[property_name] != "infinity", f"{property_name} is unbounded"
        assert abs(int(state[property_name]) - ram * percent // 100) <= page
    assert state["OOMPolicy"] == "kill"
    assert (cgroup / "memory.oom.group").read_text().strip() == "1"
    assert int(Path(f"/proc/{pid}/oom_score_adj").read_text()) >= 0
    services = essential_processes()

    env = dict(
        line.split("=", 1)
        for line in Path("/etc/exordos_metapaas/rustfs.env").read_text().splitlines()
        if line and not line.startswith("#")
    )
    client = Minio(
        "127.0.0.1:9000",
        access_key=env["RUSTFS_ACCESS_KEY"],
        secret_key=env["RUSTFS_SECRET_KEY"],
        secure=False,
        region="us-east-1",
        http_client=urllib3.PoolManager(
            timeout=urllib3.Timeout(connect=2, read=5), retries=False
        ),
    )
    bucket = f"cassi-oom-{uuid.uuid4().hex}"
    payload = os.urandom(1024 * 1024)
    client.make_bucket(bucket)
    pressure = None
    try:
        client.put_object(bucket, "before", io.BytesIO(payload), len(payload))
        before = events(cgroup)
        # Only this child's allocations are injected into the real RustFS
        # cgroup. It is killed with RustFS by memory.oom.group; the controller
        # and the platform agents remain outside it. The deadline below also
        # removes the injector if containment regresses.
        pressure = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import os, sys, time; "
                "Path(sys.argv[1], 'cgroup.procs').write_text(str(os.getpid())); "
                "blocks=[]; "
                "exec('for _ in range(int(sys.argv[2]) // (4 * 1024 * 1024)):'"
                # Fill the allocations: zero-filled calloc/mmap alone can
                # reserve virtual space without faulting in physical pages.
                "'\\n blocks.append(bytearray(b\"x\" * (4 * 1024 * 1024)))'); "
                "time.sleep(30)",
                str(cgroup),
                str(ram * 85 // 100),
            ],
        )
        deadline = time.monotonic() + 180
        while pressure.poll() is None and time.monotonic() < deadline:
            assert essential_processes() == services
            time.sleep(1)
        assert pressure.poll() == -signal.SIGKILL, "injector was not OOM-killed"
        assert events(cgroup)["oom_kill"] > before["oom_kill"]

        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            assert essential_processes() == services
            current = properties(UNIT, "MainPID", "ActiveState")
            if current["ActiveState"] == "active" and int(current["MainPID"]) != pid:
                break
            time.sleep(1)
        else:
            pytest.fail("RustFS did not restart within 90 seconds")

        with client.get_object(bucket, "before") as response:
            assert response.read() == payload
        client.put_object(bucket, "after", io.BytesIO(payload), len(payload))
        with client.get_object(bucket, "after") as response:
            assert response.read() == payload
        assert essential_processes() == services
    finally:
        if pressure is not None:
            if pressure.poll() is None:
                pressure.kill()
            pressure.wait(timeout=10)
        for key in ("before", "after"):
            client.remove_object(bucket, key)
        client.remove_bucket(bucket)
