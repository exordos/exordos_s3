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
"""Access key lifecycle tests — create, verify access, delete, verify loss."""

import time
import uuid

import botocore.exceptions
import pytest

import genesis_s3.tests.functional.conftest as s3_conftest


class TestAccessKeyCreation:
    """New access key inherits user's policies and can access S3."""

    def test_new_key_has_user_policies(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_endpoint
    ):
        bucket_name = f"key-test-{uuid.uuid4().hex[:8]}"
        s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_name, s3_project_id, s3_endpoint
        )

        # Create user + policy
        user = s3_conftest.create_user_via_api(
            s3_api_client, s3_instance_uuid, "key-test-user", s3_project_id
        )
        policy_content = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
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
            "key-test-pol",
            policy_content,
            s3_project_id,
        )
        s3_conftest.attach_policy_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], policy["uuid"], s3_project_id
        )

        # Create access key
        key = s3_conftest.create_access_key_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], s3_project_id, s3_endpoint
        )
        assert "access_key" in key
        assert "secret_key" in key

        # Verify key can access bucket
        client = s3_conftest.make_s3_client(s3_endpoint, key["access_key"], key["secret_key"])
        client.put_object(Bucket=bucket_name, Key="test", Body=b"works")
        resp = client.get_object(Bucket=bucket_name, Key="test")
        assert resp["Body"].read() == b"works"

    def test_secret_key_only_at_creation(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_endpoint
    ):
        """secret_key is visible only in the create response, not in GET."""
        user = s3_conftest.create_user_via_api(
            s3_api_client, s3_instance_uuid, "key-secret-user", s3_project_id
        )
        # Add a policy so the key can list buckets for sync verification
        policy_content = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:*"],
                    "Resource": ["arn:aws:s3:::*", "arn:aws:s3:::*/*"],
                }
            ],
        }
        policy = s3_conftest.create_policy_via_api(
            s3_api_client,
            s3_instance_uuid,
            "key-secret-pol",
            policy_content,
            s3_project_id,
        )
        s3_conftest.attach_policy_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], policy["uuid"], s3_project_id
        )
        key = s3_conftest.create_access_key_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], s3_project_id, s3_endpoint
        )

        # secret_key present in create response
        assert key.get("secret_key") is not None

        # GET the same key — secret_key should be hidden
        keys_collection = f"{s3_conftest.S3_INSTANCES}{s3_instance_uuid}/users/{user['uuid']}/keys/"
        fetched = s3_api_client.filter(keys_collection)
        matching = [k for k in fetched if k["access_key"] == key["access_key"]]
        assert len(matching) == 1
        # secret_key should not be in the GET response (field is HIDDEN)
        assert "secret_key" not in matching[0] or matching[0].get("secret_key") is None


class TestAccessKeyDeletion:
    """Deleted access key immediately loses S3 access."""

    def test_deleted_key_loses_access(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_endpoint
    ):
        bucket_name = f"key-del-{uuid.uuid4().hex[:8]}"
        s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_name, s3_project_id, s3_endpoint
        )

        user = s3_conftest.create_user_via_api(
            s3_api_client, s3_instance_uuid, "key-del-user", s3_project_id
        )
        policy_content = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:ListBucket"],
                    "Resource": [f"arn:aws:s3:::{bucket_name}"],
                }
            ],
        }
        policy = s3_conftest.create_policy_via_api(
            s3_api_client,
            s3_instance_uuid,
            "key-del-pol",
            policy_content,
            s3_project_id,
        )
        s3_conftest.attach_policy_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], policy["uuid"], s3_project_id
        )

        # Create key and verify access
        key = s3_conftest.create_access_key_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], s3_project_id, s3_endpoint
        )
        client = s3_conftest.make_s3_client(s3_endpoint, key["access_key"], key["secret_key"])
        client.list_objects_v2(Bucket=bucket_name)  # works

        # Delete key via CP API
        keys_collection = f"{s3_conftest.S3_INSTANCES}{s3_instance_uuid}/users/{user['uuid']}/keys/"
        s3_api_client.delete(keys_collection, uuid=key["uuid"])

        # Wait for dataplane sync (key deletion needs time to propagate)
        time.sleep(10)

        # Same credentials should now fail
        with pytest.raises(botocore.exceptions.ClientError) as exc_info:
            client.list_objects_v2(Bucket=bucket_name)
        assert exc_info.value.response["Error"]["Code"] in (
            "AccessDenied",
            "InvalidAccessKeyId",
            "SignatureDoesNotMatch",
        )


class TestUserDeletion:
    """Deleting a user cascades to its access keys and policy attachments."""

    def test_deleted_user_loses_access(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_endpoint
    ):
        bucket_name = f"key-userdel-{uuid.uuid4().hex[:8]}"
        s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_name, s3_project_id, s3_endpoint
        )

        user = s3_conftest.create_user_via_api(
            s3_api_client, s3_instance_uuid, "userdel-user", s3_project_id
        )
        policy_content = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:*"],
                    "Resource": ["arn:aws:s3:::*", "arn:aws:s3:::*/*"],
                }
            ],
        }
        policy = s3_conftest.create_policy_via_api(
            s3_api_client,
            s3_instance_uuid,
            "userdel-pol",
            policy_content,
            s3_project_id,
        )
        s3_conftest.attach_policy_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], policy["uuid"], s3_project_id
        )
        key = s3_conftest.create_access_key_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], s3_project_id, s3_endpoint
        )
        client = s3_conftest.make_s3_client(s3_endpoint, key["access_key"], key["secret_key"])

        # Access works before deletion
        client.list_objects_v2(Bucket=bucket_name)

        # Delete user
        users_collection = f"{s3_conftest.S3_INSTANCES}{s3_instance_uuid}/users/"
        s3_api_client.delete(users_collection, uuid=user["uuid"])

        # Wait for dataplane sync
        time.sleep(10)

        # Same credentials should now fail
        with pytest.raises(botocore.exceptions.ClientError) as exc_info:
            client.list_objects_v2(Bucket=bucket_name)
        assert exc_info.value.response["Error"]["Code"] in (
            "AccessDenied",
            "InvalidAccessKeyId",
            "SignatureDoesNotMatch",
        )


class TestMultipleAccessKeysPerUser:
    """A user can have multiple access keys, all inheriting user policies."""

    def test_multiple_keys_both_work(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_endpoint
    ):
        bucket_name = f"key-multi-{uuid.uuid4().hex[:8]}"
        s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_name, s3_project_id, s3_endpoint
        )

        user = s3_conftest.create_user_via_api(
            s3_api_client, s3_instance_uuid, "multi-key-user", s3_project_id
        )
        policy_content = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:*"],
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
            "multi-key-pol",
            policy_content,
            s3_project_id,
        )
        s3_conftest.attach_policy_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], policy["uuid"], s3_project_id
        )

        # Create two keys for the same user
        key1 = s3_conftest.create_access_key_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], s3_project_id, s3_endpoint
        )
        key2 = s3_conftest.create_access_key_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], s3_project_id, s3_endpoint
        )
        assert key1["access_key"] != key2["access_key"]

        client1 = s3_conftest.make_s3_client(
            s3_endpoint, key1["access_key"], key1["secret_key"]
        )
        client2 = s3_conftest.make_s3_client(
            s3_endpoint, key2["access_key"], key2["secret_key"]
        )

        # Both keys can access the bucket
        client1.put_object(Bucket=bucket_name, Key="from-key1", Body=b"one")
        client2.put_object(Bucket=bucket_name, Key="from-key2", Body=b"two")

        # Each key can read what the other wrote
        resp1 = client1.get_object(Bucket=bucket_name, Key="from-key2")
        assert resp1["Body"].read() == b"two"
        resp2 = client2.get_object(Bucket=bucket_name, Key="from-key1")
        assert resp2["Body"].read() == b"one"
