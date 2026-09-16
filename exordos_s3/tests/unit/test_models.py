#    Copyright 2025-2026 Genesis Corporation.
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

import typing as tp
import uuid as sys_uuid

import pytest

from exordos_s3.controlplane.dm import models


def _instance(**kwargs: tp.Any) -> models.S3Instance:
    params = {
        "name": "s3",
        "project_id": sys_uuid.uuid4(),
        "cpu": 1,
        "ram": 1024,
        "disk_size": 10,
        "nodes_number": 1,
        "version": models.S3Version(name="rustfs", image="http://image"),
    }
    params.update(kwargs)
    return models.S3Instance(**params)


def _distributed(**kwargs: tp.Any) -> models.S3Instance:
    return _instance(
        kind=models.S3InstanceKind.DISTRIBUTED.value, **{"nodes_number": 4, **kwargs}
    )


class TestInstanceKind:
    def test_single_node_requires_one_node(self) -> None:
        with pytest.raises(models.S3ValidationError, match="nodes_number=1"):
            _instance(nodes_number=2)._validate_kind()

    def test_single_node_has_no_parity(self) -> None:
        with pytest.raises(models.S3ValidationError, match="parity"):
            _instance(parity=1)._validate_kind()

    @pytest.mark.parametrize("nodes_number", [4, 7, 16])
    def test_distributed_nodes_number(self, nodes_number: int) -> None:
        _distributed(nodes_number=nodes_number)._validate_kind()

    @pytest.mark.parametrize("nodes_number", [1, 3])
    def test_distributed_needs_four_nodes(self, nodes_number: int) -> None:
        with pytest.raises(models.S3ValidationError, match="between 4 and 16"):
            _distributed(nodes_number=nodes_number)._validate_kind()

    @pytest.mark.parametrize(
        ("nodes_number", "parity"), [(4, None), (4, 1), (4, 2), (7, 3)]
    )
    def test_distributed_parity(self, nodes_number: int, parity: int | None) -> None:
        _distributed(nodes_number=nodes_number, parity=parity)._validate_kind()

    @pytest.mark.parametrize(("nodes_number", "parity"), [(4, 3), (7, 4)])
    def test_parity_is_at_most_half_of_nodes(
        self, nodes_number: int, parity: int
    ) -> None:
        with pytest.raises(models.S3ValidationError, match="half"):
            _distributed(nodes_number=nodes_number, parity=parity)._validate_kind()


class TestInstanceUpdate:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("kind", models.S3InstanceKind.SINGLE_NODE.value),
            ("nodes_number", 5),
            ("parity", 2),
            ("cpu", 2),
            ("ram", 2048),
        ],
    )
    def test_distributed_layout_is_immutable(self, field: str, value: tp.Any) -> None:
        instance = _distributed(parity=1)
        setattr(instance, field, value)

        with pytest.raises(models.S3ValidationError, match=f"{field} can't be changed"):
            instance._validate_update()

    def test_distributed_disk_grows(self) -> None:
        instance = _distributed()
        instance.disk_size = 20
        instance.members = {"node": {"ordinal": 1, "ipv4": "10.0.0.1"}}

        instance._validate_update()

    def test_disk_does_not_shrink(self) -> None:
        instance = _distributed()
        instance.disk_size = 9

        with pytest.raises(models.S3ValidationError, match="shrink"):
            instance._validate_update()

    def test_single_node_resources_change(self) -> None:
        instance = _instance()
        instance.cpu = 2
        instance.ram = 2048

        instance._validate_update()


class TestBucketNameType:
    @pytest.fixture
    def bucket_name(self) -> models.BucketNameType:
        return models.BucketNameType()

    @pytest.mark.parametrize(
        "name",
        [
            "abc",
            "my-bucket",
            "my.bucket.name",
            "bucket123",
            "1-2-3",
            "a" * 63,
            # AWS reserves these, RustFS does not, so they are accepted here.
            "xn--bucket",
            "sthree-bucket",
            "bucket-s3alias",
        ],
    )
    def test_valid_names(self, bucket_name: models.BucketNameType, name: str) -> None:
        assert bucket_name.validate(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "world_bucket",  # underscore
            "ab",  # too short
            "a" * 64,  # too long
            "MyBucket",  # uppercase
            "-bucket",  # leading hyphen
            "bucket-",  # trailing hyphen
            ".bucket",  # leading dot
            "bucket.",  # trailing dot
            "my..bucket",  # consecutive dots
            "my.-bucket",  # empty label
            "192.168.0.1",  # IPv4 address
            "bucket name",  # space
            "bucket\n",  # trailing newline
            None,
            123,
        ],
    )
    def test_invalid_names(
        self, bucket_name: models.BucketNameType, name: tp.Any
    ) -> None:
        assert bucket_name.validate(name) is False
