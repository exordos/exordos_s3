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

"""Verify the offline API specification and element artifact contract."""

import os
from pathlib import Path
import sys
from unittest import mock

import yaml

from exordos_s3 import openapi
from exordos_s3 import openapi_spec


def test_build_covers_only_s3_routes_and_preserves_field_visibility() -> None:
    spec = openapi.build()
    assert spec["openapi"] == "3.0.3"
    assert spec["info"]["version"] == "latest"
    assert spec["paths"]
    assert all(path.startswith("/v1/types/s3/") for path in spec["paths"])
    for collection in ("versions", "instances"):
        assert f"/v1/types/s3/{collection}/" in spec["paths"]
    for collection in ("buckets", "policies", "users"):
        assert any(path.endswith(f"/{collection}/") for path in spec["paths"])
    assert any(path.endswith("/keys/") for path in spec["paths"])
    schemas = spec["components"]["schemas"]
    assert "S3Instance_Get" in schemas
    assert "S3AccessKey_Get" in schemas
    for name, schema in schemas.items():
        if name.startswith("S3Instance_") and "properties" in schema:
            assert "members" not in schema["properties"]
            assert "root_secret" not in schema["properties"]
        if name == "S3AccessKey_Get":
            assert "secret_key" not in schema["properties"]

    def check_references(node: object) -> None:
        if isinstance(node, dict):
            if "$ref" in node:
                target = spec
                assert node["$ref"].startswith("#/")
                for part in node["$ref"][2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            for value in node.values():
                check_references(value)
        elif isinstance(node, list):
            for value in node:
                check_references(value)

    check_references(spec)
    for schema in schemas.values():
        for name in ("access_key", "secret_key", "root_secret"):
            assert "default" not in schema.get("properties", {}).get(name, {})
    for parameter in spec["components"]["parameters"].values():
        if parameter.get("name") in ("access_key", "secret_key", "root_secret"):
            assert "default" not in parameter["schema"]


def test_main_writes_yaml_without_changing_description_text(tmp_path: Path) -> None:
    spec = openapi.build()
    with (
        mock.patch.object(sys, "argv", ["openapi-spec", "-o", str(tmp_path)]),
        mock.patch.object(openapi, "build", return_value=spec),
    ):
        openapi_spec.main()
    saved = yaml.safe_load((tmp_path / "openapi_user.yaml").read_text())
    assert saved == spec


def test_s3aas_build_generates_spec_before_packaging() -> None:
    root = Path(__file__).parents[3]
    config = yaml.safe_load((root / "exordos/exordos.yaml").read_text())
    element = next(
        item
        for item in config["build"]["elements"]
        if item["manifest"] == "manifests/s3aas.yaml.j2"
    )
    artifact = next(
        item
        for item in element["artifacts"]
        if item.get("script") == "images/build_openapi_spec.sh"
    )
    script = root / "exordos" / artifact["script"]
    assert script.is_file()
    assert os.access(script, os.X_OK)
    assert artifact["artifacts"] == ["../docs/openapi/openapi_user.yaml"]
