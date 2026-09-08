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

import pytest

from exordos_s3.controlplane.dm import models


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
            "xn--bucket",  # reserved prefix
            "sthree-bucket",  # reserved prefix
            "bucket-s3alias",  # reserved suffix
            "bucket--ol-s3",  # reserved suffix
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
