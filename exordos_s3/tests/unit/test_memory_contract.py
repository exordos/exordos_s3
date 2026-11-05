#    Copyright 2026 Genesis Corporation.
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
from pathlib import Path

import pytest


def _service() -> configparser.SectionProxy:
    unit = configparser.ConfigParser(interpolation=None)
    unit.optionxform = str
    unit.read(
        Path(__file__).resolve().parents[3]
        / "etc/systemd/exordos-metapaas-rustfs.service"
    )
    return unit["Service"]


@pytest.mark.parametrize("setting,percent", [("MemoryHigh", 55), ("MemoryMax", 60)])
def test_rustfs_reserves_guest_memory(setting: str, percent: int) -> None:
    # systemd resolves percentages against the guest's physical RAM, including
    # after a VM resize; an absolute byte count would retain the old budget.
    assert _service().get(setting) == f"{percent}%"


def test_rustfs_can_be_killed_and_restarts_as_a_group() -> None:
    service = _service()

    assert int(service.get("OOMScoreAdjust", "0")) >= 0
    assert service.get("OOMPolicy") == "kill"
    assert service["Restart"] == "always"
