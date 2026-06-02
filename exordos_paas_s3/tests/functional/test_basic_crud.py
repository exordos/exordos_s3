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
"""Basic CRUD integration tests — instance lifecycle, bucket creation, and
S3 data operations."""

import time
import uuid

import botocore.exceptions
import pytest
import requests

import exordos_paas_s3.tests.functional.conftest as s3_conftest


class TestInstanceLifecycle:
    """Instance creation, status polling, and teardown."""

    def test_instance_is_active(self, s3_instance):
        assert s3_instance["status"] == "ACTIVE"

    def test_instance_has_ips(self, s3_instance):
        ips = s3_instance.get("ipsv4", [])
        assert len(ips) >= 1, "Instance should have at least one IP"

    def test_instance_kind(self, s3_instance):
        assert s3_instance["kind"] == "single_node"

    def test_instance_nodes_number(self, s3_instance):
        assert s3_instance["nodes_number"] == 1


class TestBucketCRUD:
    """Bucket creation via CP API and verification via S3 data API."""

    def test_create_bucket(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_clients, s3_endpoint
    ):
        bucket_name = f"test-bucket-{uuid.uuid4().hex[:8]}"
        bucket = s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_name, s3_project_id, s3_endpoint
        )
        assert bucket["name"] == bucket_name
        assert bucket["status"] == "ACTIVE"

        # Verify bucket appears in S3 ListBuckets
        client = list(s3_clients.values())[0]
        resp = client.list_buckets()
        bucket_names = [b["Name"] for b in resp.get("Buckets", [])]
        assert bucket_name in bucket_names

    def test_create_versioned_bucket(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_clients, s3_endpoint
    ):
        bucket_name = f"test-ver-{uuid.uuid4().hex[:8]}"
        bucket = s3_conftest.create_bucket_via_api(
            s3_api_client,
            s3_instance_uuid,
            bucket_name,
            s3_project_id,
            s3_endpoint,
            versioning_enabled=True,
        )
        assert bucket["versioning_enabled"] is True

        # Verify versioning via S3 API
        client = list(s3_clients.values())[0]
        ver = client.get_bucket_versioning(Bucket=bucket_name)
        assert ver.get("Status") == "Enabled"

    def test_create_object_lock_bucket(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_clients, s3_endpoint
    ):
        bucket_name = f"test-lock-{uuid.uuid4().hex[:8]}"
        bucket = s3_conftest.create_bucket_via_api(
            s3_api_client,
            s3_instance_uuid,
            bucket_name,
            s3_project_id,
            s3_endpoint,
            versioning_enabled=True,
            object_lock_enabled=True,
            default_retention_mode="COMPLIANCE",
            default_retention_days=30,
        )
        assert bucket["object_lock_enabled"] is True

        # Verify object lock config via S3 API
        client = list(s3_clients.values())[0]
        lock = client.get_object_lock_configuration(Bucket=bucket_name)
        config = lock.get("ObjectLockConfiguration", {})
        assert config.get("ObjectLockEnabled") == "Enabled"

    def test_delete_bucket(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_clients, s3_endpoint
    ):
        bucket_name = f"test-del-{uuid.uuid4().hex[:8]}"
        bucket = s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_name, s3_project_id, s3_endpoint
        )

        # Delete via CP API
        collection = f"{s3_conftest.S3_INSTANCES}{s3_instance_uuid}/buckets/"
        s3_api_client.delete(collection, uuid=bucket["uuid"])

        # Wait for dataplane sync (bucket deletion needs time to propagate)
        time.sleep(10)

        # Verify bucket gone from S3
        client = list(s3_clients.values())[0]
        resp = client.list_buckets()
        bucket_names = [b["Name"] for b in resp.get("Buckets", [])]
        assert bucket_name not in bucket_names


class TestS3DataOperations:
    """Basic S3 data operations: put, get, list, delete objects."""

    def test_put_and_get_object(
        self, s3_clients, s3_api_client, s3_instance_uuid, s3_project_id, s3_endpoint
    ):
        bucket_name = f"test-data-{uuid.uuid4().hex[:8]}"
        s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_name, s3_project_id, s3_endpoint
        )

        client = list(s3_clients.values())[0]
        content = b"hello integration test"
        key = s3_conftest.upload_test_object(client, bucket_name, "test-key", content)
        downloaded = s3_conftest.download_object(client, bucket_name, key)
        assert downloaded == content

    def test_list_objects(
        self, s3_clients, s3_api_client, s3_instance_uuid, s3_project_id, s3_endpoint
    ):
        bucket_name = f"test-list-{uuid.uuid4().hex[:8]}"
        s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_name, s3_project_id, s3_endpoint
        )

        client = list(s3_clients.values())[0]
        s3_conftest.upload_test_object(client, bucket_name, "obj1")
        s3_conftest.upload_test_object(client, bucket_name, "obj2")

        resp = client.list_objects_v2(Bucket=bucket_name)
        keys = [o["Key"] for o in resp.get("Contents", [])]
        assert "obj1" in keys
        assert "obj2" in keys

    def test_delete_object(
        self, s3_clients, s3_api_client, s3_instance_uuid, s3_project_id, s3_endpoint
    ):
        bucket_name = f"test-delobj-{uuid.uuid4().hex[:8]}"
        s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_name, s3_project_id, s3_endpoint
        )

        client = list(s3_clients.values())[0]
        s3_conftest.upload_test_object(client, bucket_name, "to-delete")

        client.delete_object(Bucket=bucket_name, Key="to-delete")

        resp = client.list_objects_v2(Bucket=bucket_name)
        keys = [o["Key"] for o in resp.get("Contents", [])]
        assert "to-delete" not in keys

    def test_bucket_public_access(
        self, s3_clients, s3_api_client, s3_instance_uuid, s3_project_id, s3_endpoint
    ):
        """Public bucket allows unauthenticated read."""
        bucket_name = f"test-pub-{uuid.uuid4().hex[:8]}"
        s3_conftest.create_bucket_via_api(
            s3_api_client,
            s3_instance_uuid,
            bucket_name,
            s3_project_id,
            s3_endpoint,
            public=True,
        )

        # Upload via authenticated client
        client = list(s3_clients.values())[0]
        s3_conftest.upload_test_object(
            client, bucket_name, "public-obj", b"public-data"
        )

        # Anonymous GET on public bucket should work
        url = f"http://{s3_endpoint}/{bucket_name}/public-obj"
        resp = requests.get(url, timeout=10)
        assert resp.status_code == 200
        assert resp.content == b"public-data"


class TestQuotaEnforcement:
    """Uploading beyond quota_bytes should fail."""

    @pytest.mark.skipif(
        False,
        reason="RustFS does not enforce quota_bytes yet",
    )
    def test_upload_beyond_quota_denied(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_clients, s3_endpoint
    ):
        bucket_name = f"test-quota-{uuid.uuid4().hex[:8]}"
        # 1 KB quota
        s3_conftest.create_bucket_via_api(
            s3_api_client,
            s3_instance_uuid,
            bucket_name,
            s3_project_id,
            s3_endpoint,
            quota_bytes=1024,
        )

        client = list(s3_clients.values())[0]

        # Small upload should succeed
        client.put_object(Bucket=bucket_name, Key="small", Body=b"x" * 100)

        # Large upload should fail
        with pytest.raises(botocore.exceptions.ClientError) as exc_info:
            client.put_object(Bucket=bucket_name, Key="big", Body=b"x" * 2048)
        assert "Bucket quota exceeded" in exc_info.value.response["Error"]["Message"]


class TestObjectLockRetention:
    """Objects under retention cannot be deleted until retention expires.

    NOTE: RustFS may not enforce object lock retention yet. This test
    documents the expected behavior and will pass once dataplane support
    is added.
    """

    @pytest.mark.skipif(
        True,
        reason="RustFS does not enforce object lock retention yet",
    )
    def test_retention_prevents_delete(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_clients, s3_endpoint
    ):
        bucket_name = f"test-ret-{uuid.uuid4().hex[:8]}"
        s3_conftest.create_bucket_via_api(
            s3_api_client,
            s3_instance_uuid,
            bucket_name,
            s3_project_id,
            s3_endpoint,
            versioning_enabled=True,
            object_lock_enabled=True,
            default_retention_mode="COMPLIANCE",
            default_retention_days=365,
        )

        client = list(s3_clients.values())[0]
        client.put_object(
            Bucket=bucket_name,
            Key="locked-obj",
            Body=b"protected",
        )

        # Delete should fail due to retention
        with pytest.raises(botocore.exceptions.ClientError) as exc_info:
            client.delete_object(Bucket=bucket_name, Key="locked-obj")
        assert (
            "AccessDenied" in exc_info.value.response["Error"]["Code"]
            or "InvalidArgument" in exc_info.value.response["Error"]["Code"]
            or "ObjectLocked" in exc_info.value.response["Error"]["Code"]
        )


class TestBucketROFields:
    """Read-only bucket fields cannot be updated via CP API."""

    def test_bucket_name_read_only(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_clients, s3_endpoint
    ):
        bucket_name = f"test-ro-{uuid.uuid4().hex[:8]}"
        bucket = s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_name, s3_project_id, s3_endpoint
        )

        # Attempt to update name should fail or be ignored
        collection = f"{s3_conftest.S3_INSTANCES}{s3_instance_uuid}/buckets/"
        with pytest.raises(Exception):
            s3_api_client.update(collection, uuid=bucket["uuid"], name="new-name")

    def test_versioning_enabled_read_only(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_clients, s3_endpoint
    ):
        bucket_name = f"test-rover-{uuid.uuid4().hex[:8]}"
        bucket = s3_conftest.create_bucket_via_api(
            s3_api_client,
            s3_instance_uuid,
            bucket_name,
            s3_project_id,
            s3_endpoint,
            versioning_enabled=False,
        )

        # Attempt to update versioning_enabled should fail or be ignored
        collection = f"{s3_conftest.S3_INSTANCES}{s3_instance_uuid}/buckets/"
        with pytest.raises(Exception):
            s3_api_client.update(
                collection, uuid=bucket["uuid"], versioning_enabled=True
            )


class TestInstanceDiskSizeUpdate:
    """Instance disk_size can be increased but not shrunk."""

    def test_disk_size_grow_ok(self, s3_api_client, s3_instance_uuid):
        collection = s3_conftest.S3_INSTANCES
        instance = s3_api_client.get(collection, uuid=s3_instance_uuid)
        old_size = instance.get("disk_size", 0)
        new_size = old_size + 10
        s3_api_client.update(collection, uuid=s3_instance_uuid, disk_size=new_size)
        updated = s3_api_client.get(collection, uuid=s3_instance_uuid)
        assert updated["disk_size"] == new_size

    def test_disk_size_shrink_fails(self, s3_api_client, s3_instance_uuid):
        collection = s3_conftest.S3_INSTANCES
        instance = s3_api_client.get(collection, uuid=s3_instance_uuid)
        old_size = instance.get("disk_size", 0)
        shrink_size = max(old_size - 1, 1)
        with pytest.raises(Exception):
            s3_api_client.update(
                collection, uuid=s3_instance_uuid, disk_size=shrink_size
            )
