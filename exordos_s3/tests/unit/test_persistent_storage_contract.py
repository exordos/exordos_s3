#    Copyright 2026 Genesis Corporation.
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

import configparser
import os
from pathlib import Path
import shlex
import subprocess


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RUSTFS_DATA_PATH = "/var/lib/rustfs/data"


def _unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(REPOSITORY_ROOT / "etc/systemd" / name)
    return parser


def test_image_install_enables_but_does_not_start_dataplane_agent() -> None:
    commands = [
        shlex.split(line.removeprefix("sudo "))
        for line in (
            REPOSITORY_ROOT / "exordos/images/dp_install.sh"
        ).read_text().splitlines()
        if line.startswith("sudo systemctl")
    ]
    agent_commands = [
        command
        for command in commands
        if "exordos-metapaas-s3-agent" in command
    ]

    assert agent_commands == [
        ["systemctl", "enable", "exordos-metapaas-s3-agent"]
    ]


def test_dataplane_agent_waits_for_bootstrap() -> None:
    unit = _unit("exordos-metapaas-s3-agent.service")

    assert "exordos-bootstrap.service" in unit["Unit"]["Wants"].split()
    assert "exordos-bootstrap.service" in unit["Unit"]["After"].split()


def test_rustfs_requires_the_persistent_data_mount() -> None:
    unit = _unit("exordos-metapaas-rustfs.service")

    assert unit["Unit"]["RequiresMountsFor"] == RUSTFS_DATA_PATH
    assert unit["Unit"]["AssertPathIsMountPoint"] == RUSTFS_DATA_PATH


def test_bootstrap_starts_rustfs_after_persistent_data_migration(
    tmp_path: Path,
) -> None:
    call_log = tmp_path / "calls.log"
    bootstrap_lib = tmp_path / "lib_bootstrap.sh"
    bootstrap_lib.write_text(
        """
record() { printf '%s\\n' "$*" >> "$CALL_LOG"; }
find_persistent_disk() { record find_persistent_disk; printf '/dev/test-disk\\n'; }
prepare_persistent_disk() { record prepare_persistent_disk "$@"; }
migrate_to_persistent_restart() { record migrate_to_persistent_restart "$@"; }
migrate_to_persistent_stop_start() { record migrate_to_persistent_stop_start "$@"; }
persist_migrate_complete() { record persist_migrate_complete; }
""".strip()
        + "\n"
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sudo = fake_bin / "sudo"
    sudo.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'sudo %s\\n' \"$*\" >> \"$CALL_LOG\"\n"
    )
    sudo.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "CALL_LOG": str(call_log),
            "EXORDOS_BOOTSTRAP_LIB": str(bootstrap_lib),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "PERSISTENT_MOUNT": "/mnt/persistent",
        }
    )
    subprocess.run(
        ["bash", str(REPOSITORY_ROOT / "exordos/images/dp_bootstrap.sh")],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )

    assert call_log.read_text().splitlines() == [
        "find_persistent_disk",
        "prepare_persistent_disk /dev/test-disk /mnt/persistent xfs",
        (
            "migrate_to_persistent_restart /var/log "
            "/mnt/persistent/var/log systemd-journald rsyslog"
        ),
        (
            "migrate_to_persistent_stop_start /var/lib/rustfs/data "
            "/mnt/persistent/var/lib/rustfs/data exordos-metapaas-rustfs"
        ),
        "persist_migrate_complete",
        "sudo systemctl enable --now exordos-metapaas-rustfs",
    ]
