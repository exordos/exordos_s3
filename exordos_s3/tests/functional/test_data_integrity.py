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
"""Data integrity tests — verify that updating bucket or policy settings
does not cause data loss."""

import time
import uuid

import botocore.exceptions
import pytest

import exordos_s3.tests.functional.conftest as s3_conftest


class TestBucketUpdatePreservesData:
    """Updating bucket settings should not delete existing objects."""

    def test_toggle_public_preserves_objects(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_clients, s3_endpoint
    ):
        bucket_name = f"int-pub-{uuid.uuid4().hex[:8]}"
        bucket = s3_conftest.create_bucket_via_api(
            s3_api_client,
            s3_instance_uuid,
            bucket_name,
            s3_project_id,
            s3_endpoint,
            public=False,
        )
        bucket_uuid = bucket["uuid"]

        # Upload data
        client = list(s3_clients.values())[0]
        content = b"survive-the-update"
        s3_conftest.upload_test_object(client, bucket_name, "precious", content)

        # Update bucket: toggle public
        collection = f"{s3_conftest.S3_INSTANCES}{s3_instance_uuid}/buckets/"
        s3_api_client.update(collection, uuid=bucket_uuid, public=True)

        # Verify data still exists
        downloaded = s3_conftest.download_object(client, bucket_name, "precious")
        assert downloaded == content

        # Revert public flag
        s3_api_client.update(collection, uuid=bucket_uuid, public=False)

        # Data still exists after revert
        downloaded = s3_conftest.download_object(client, bucket_name, "precious")
        assert downloaded == content

    def test_quota_update_preserves_objects(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_clients, s3_endpoint
    ):
        bucket_name = f"int-quota-{uuid.uuid4().hex[:8]}"
        bucket = s3_conftest.create_bucket_via_api(
            s3_api_client,
            s3_instance_uuid,
            bucket_name,
            s3_project_id,
            s3_endpoint,
            quota_bytes=0,
        )
        bucket_uuid = bucket["uuid"]

        # Upload data
        client = list(s3_clients.values())[0]
        content = b"survive-quota-update"
        s3_conftest.upload_test_object(client, bucket_name, "data", content)

        # Update quota
        collection = f"{s3_conftest.S3_INSTANCES}{s3_instance_uuid}/buckets/"
        s3_api_client.update(collection, uuid=bucket_uuid, quota_bytes=104857600)

        # Verify data still exists
        downloaded = s3_conftest.download_object(client, bucket_name, "data")
        assert downloaded == content


class TestPolicyUpdatePreservesAccess:
    """Updating policy content should take effect without breaking existing
    access patterns."""

    def test_add_permission_to_policy(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_endpoint
    ):
        bucket_name = f"int-pol-{uuid.uuid4().hex[:8]}"
        s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_name, s3_project_id, s3_endpoint
        )

        # Create user with read-only policy
        user = s3_conftest.create_user_via_api(
            s3_api_client, s3_instance_uuid, "int-pol-user", s3_project_id
        )
        readonly_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:ListBucket"],
                    "Resource": [
                        f"arn:aws:s3:::{bucket_name}",
                        f"arn:aws:s3:::{bucket_name}/*",
                    ],
                }
            ],
        }
        policy = s3_conftest.create_policy_via_api(
            s3_api_client,
            s3_instance_uuid,
            "int-pol-ro",
            readonly_policy,
            s3_project_id,
        )
        s3_conftest.attach_policy_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], policy["uuid"], s3_project_id
        )
        key = s3_conftest.create_access_key_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], s3_project_id, s3_endpoint
        )
        client = s3_conftest.make_s3_client(
            s3_endpoint, key["access_key"], key["secret_key"]
        )

        # PutObject should fail with read-only
        with pytest.raises(botocore.exceptions.ClientError) as exc_info:
            client.put_object(Bucket=bucket_name, Key="obj", Body=b"nope")
        assert exc_info.value.response["Error"]["Code"] in (
            "AccessDenied",
            "AllAccessDisabled",
        )

        # Update policy to add write permissions
        readwrite_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:GetObject",
                        "s3:PutObject",
                        "s3:DeleteObject",
                        "s3:ListBucket",
                    ],
                    "Resource": [
                        f"arn:aws:s3:::{bucket_name}",
                        f"arn:aws:s3:::{bucket_name}/*",
                    ],
                }
            ],
        }
        collection = f"{s3_conftest.S3_INSTANCES}{s3_instance_uuid}/policies/"
        s3_api_client.update(collection, uuid=policy["uuid"], content=readwrite_policy)

        # Wait for dataplane sync (policy update needs time to propagate)
        time.sleep(10)

        # Now PutObject should succeed
        client.put_object(Bucket=bucket_name, Key="obj", Body=b"now-allowed")
        resp = client.get_object(Bucket=bucket_name, Key="obj")
        assert resp["Body"].read() == b"now-allowed"
