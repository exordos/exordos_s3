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
"""Listing a bucket whose directories share a name prefix."""

import concurrent.futures
import uuid

import exordos_s3.tests.functional.conftest as s3_conftest

# DBaaS backups keep "<id>" and "<id>-rollbacks" side by side. In keys "-"
# sorts before "/", so "s-x/..." lists ahead of "s/...", against the order the
# directories are walked in.
DIRECTORIES = ("s", "s-x")
KEYS_PER_DIRECTORY = 1200


class TestFlatListing:
    def test_flat_listing_returns_every_key(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_probe_client
    ):
        # RustFS 1.0.0 and 1.0.1-preview.9 return 2000 of these 2400 keys and
        # answer IsTruncated=false; a bucket of DBaaS backups listed 5000
        # of its 104137.
        bucket_name = f"test-list-{uuid.uuid4().hex[:8]}"
        s3_conftest.create_bucket_via_api(
            s3_api_client,
            s3_instance_uuid,
            bucket_name,
            s3_project_id,
            s3_probe_client,
        )
        keys = [
            f"db/backup/{directory}/l/l/{number:05d}.zst"
            for directory in DIRECTORIES
            for number in range(KEYS_PER_DIRECTORY)
        ]

        with concurrent.futures.ThreadPoolExecutor(16) as pool:
            list(
                pool.map(
                    lambda key: s3_probe_client.put_object(
                        Bucket=bucket_name, Key=key, Body=b"x"
                    ),
                    keys,
                )
            )

        listed = [
            obj["Key"]
            for page in s3_probe_client.get_paginator("list_objects_v2").paginate(
                Bucket=bucket_name
            )
            for obj in page.get("Contents", [])
        ]
        assert sorted(listed) == sorted(keys)
