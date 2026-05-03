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
"""Policy enforcement integration tests — verify IAM policies control
access to buckets as expected."""

import time
import uuid

import botocore.exceptions
import pytest

import exordos_s3.tests.functional.conftest as s3_conftest


def _make_readwrite_policy(bucket_name: str) -> dict:
    """Create a policy allowing read/write on a specific bucket."""
    return {
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


def _make_readonly_policy(bucket_name: str) -> dict:
    """Create a policy allowing only read on a specific bucket."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:ListBucket",
                ],
                "Resource": [
                    f"arn:aws:s3:::{bucket_name}",
                    f"arn:aws:s3:::{bucket_name}/*",
                ],
            }
        ],
    }


class TestPolicyAllowsOwnBucket:
    """User with a policy on bucket A can operate on bucket A."""

    def test_put_get_delete_allowed(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_endpoint
    ):
        bucket_name = f"pol-own-{uuid.uuid4().hex[:8]}"
        s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_name, s3_project_id, s3_endpoint
        )

        # Create user + policy + key
        user = s3_conftest.create_user_via_api(
            s3_api_client, s3_instance_uuid, "pol-test-user-1", s3_project_id
        )
        policy = s3_conftest.create_policy_via_api(
            s3_api_client,
            s3_instance_uuid,
            "pol-test-rw",
            _make_readwrite_policy(bucket_name),
            s3_project_id,
        )
        s3_conftest.attach_policy_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], policy["uuid"], s3_project_id
        )
        key = s3_conftest.create_access_key_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], s3_project_id, s3_endpoint
        )
        client = s3_conftest.make_s3_client(s3_endpoint, key["access_key"], key["secret_key"])

        # All operations should succeed
        client.put_object(Bucket=bucket_name, Key="test-obj", Body=b"data")
        resp = client.get_object(Bucket=bucket_name, Key="test-obj")
        assert resp["Body"].read() == b"data"
        client.list_objects_v2(Bucket=bucket_name)
        client.delete_object(Bucket=bucket_name, Key="test-obj")


class TestPolicyDeniesOtherBucket:
    """User with a policy on bucket A cannot access bucket B."""

    def test_access_denied_on_unauthorized_bucket(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_endpoint
    ):
        bucket_a = f"pol-a-{uuid.uuid4().hex[:8]}"
        bucket_b = f"pol-b-{uuid.uuid4().hex[:8]}"
        s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_a, s3_project_id, s3_endpoint
        )
        s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_b, s3_project_id, s3_endpoint
        )

        # User has policy on bucket_a only
        user = s3_conftest.create_user_via_api(
            s3_api_client, s3_instance_uuid, "pol-test-user-2", s3_project_id
        )
        policy = s3_conftest.create_policy_via_api(
            s3_api_client,
            s3_instance_uuid,
            "pol-test-a-only",
            _make_readwrite_policy(bucket_a),
            s3_project_id,
        )
        s3_conftest.attach_policy_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], policy["uuid"], s3_project_id
        )
        key = s3_conftest.create_access_key_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], s3_project_id, s3_endpoint
        )
        client = s3_conftest.make_s3_client(s3_endpoint, key["access_key"], key["secret_key"])

        # PutObject on bucket_a succeeds
        client.put_object(Bucket=bucket_a, Key="obj", Body=b"ok")

        # PutObject on bucket_b fails with AccessDenied
        with pytest.raises(botocore.exceptions.ClientError) as exc_info:
            client.put_object(Bucket=bucket_b, Key="obj", Body=b"nope")
        assert exc_info.value.response["Error"]["Code"] in (
            "AccessDenied",
            "AllAccessDisabled",
        )

        # GetObject on bucket_b also fails
        with pytest.raises(botocore.exceptions.ClientError) as exc_info:
            client.get_object(Bucket=bucket_b, Key="obj")
        assert exc_info.value.response["Error"]["Code"] in (
            "AccessDenied",
            "AllAccessDisabled",
            "NoSuchKey",
        )


class TestReadOnlyPolicy:
    """User with read-only policy cannot write or delete."""

    def test_readonly_cannot_write(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_endpoint
    ):
        bucket_name = f"pol-ro-{uuid.uuid4().hex[:8]}"
        s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_name, s3_project_id, s3_endpoint
        )

        user = s3_conftest.create_user_via_api(
            s3_api_client, s3_instance_uuid, "pol-ro-user", s3_project_id
        )
        policy = s3_conftest.create_policy_via_api(
            s3_api_client,
            s3_instance_uuid,
            "pol-readonly",
            _make_readonly_policy(bucket_name),
            s3_project_id,
        )
        s3_conftest.attach_policy_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], policy["uuid"], s3_project_id
        )
        key = s3_conftest.create_access_key_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], s3_project_id, s3_endpoint
        )
        client = s3_conftest.make_s3_client(s3_endpoint, key["access_key"], key["secret_key"])

        # ListBucket and GetObject should work (if objects exist)
        client.list_objects_v2(Bucket=bucket_name)

        # PutObject should fail
        with pytest.raises(botocore.exceptions.ClientError) as exc_info:
            client.put_object(Bucket=bucket_name, Key="obj", Body=b"nope")
        assert exc_info.value.response["Error"]["Code"] in (
            "AccessDenied",
            "AllAccessDisabled",
        )

        # DeleteObject should fail
        with pytest.raises(botocore.exceptions.ClientError) as exc_info:
            client.delete_object(Bucket=bucket_name, Key="obj")
        assert exc_info.value.response["Error"]["Code"] in (
            "AccessDenied",
            "AllAccessDisabled",
        )


class TestSeparateUserAccess:
    """Two users with separate policies cannot access each other's buckets."""

    def test_cross_user_isolation(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_endpoint
    ):
        bucket_x = f"pol-x-{uuid.uuid4().hex[:8]}"
        bucket_y = f"pol-y-{uuid.uuid4().hex[:8]}"
        s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_x, s3_project_id, s3_endpoint
        )
        s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_y, s3_project_id, s3_endpoint
        )

        # User X has access to bucket_x
        user_x = s3_conftest.create_user_via_api(
            s3_api_client, s3_instance_uuid, "user-x", s3_project_id
        )
        policy_x = s3_conftest.create_policy_via_api(
            s3_api_client,
            s3_instance_uuid,
            "pol-x-rw",
            _make_readwrite_policy(bucket_x),
            s3_project_id,
        )
        s3_conftest.attach_policy_via_api(
            s3_api_client,
            s3_instance_uuid,
            user_x["uuid"],
            policy_x["uuid"],
            s3_project_id,
        )
        key_x = s3_conftest.create_access_key_via_api(
            s3_api_client, s3_instance_uuid, user_x["uuid"], s3_project_id, s3_endpoint
        )
        client_x = s3_conftest.make_s3_client(s3_endpoint, key_x["access_key"], key_x["secret_key"])

        # User Y has access to bucket_y
        user_y = s3_conftest.create_user_via_api(
            s3_api_client, s3_instance_uuid, "user-y", s3_project_id
        )
        policy_y = s3_conftest.create_policy_via_api(
            s3_api_client,
            s3_instance_uuid,
            "pol-y-rw",
            _make_readwrite_policy(bucket_y),
            s3_project_id,
        )
        s3_conftest.attach_policy_via_api(
            s3_api_client,
            s3_instance_uuid,
            user_y["uuid"],
            policy_y["uuid"],
            s3_project_id,
        )
        key_y = s3_conftest.create_access_key_via_api(
            s3_api_client, s3_instance_uuid, user_y["uuid"], s3_project_id, s3_endpoint
        )
        client_y = s3_conftest.make_s3_client(s3_endpoint, key_y["access_key"], key_y["secret_key"])

        # User X can write to bucket_x
        client_x.put_object(Bucket=bucket_x, Key="x-obj", Body=b"x-data")

        # User X cannot write to bucket_y
        with pytest.raises(botocore.exceptions.ClientError) as exc_info:
            client_x.put_object(Bucket=bucket_y, Key="intruder", Body=b"nope")
        assert exc_info.value.response["Error"]["Code"] in (
            "AccessDenied",
            "AllAccessDisabled",
        )

        # User Y can write to bucket_y
        client_y.put_object(Bucket=bucket_y, Key="y-obj", Body=b"y-data")

        # User Y cannot write to bucket_x
        with pytest.raises(botocore.exceptions.ClientError) as exc_info:
            client_y.put_object(Bucket=bucket_x, Key="intruder", Body=b"nope")
        assert exc_info.value.response["Error"]["Code"] in (
            "AccessDenied",
            "AllAccessDisabled",
        )


class TestDetachPolicy:
    """Detaching a policy from a user revokes the access it granted."""

    def test_detach_revokes_access(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_endpoint
    ):
        bucket_name = f"pol-detach-{uuid.uuid4().hex[:8]}"
        s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_name, s3_project_id, s3_endpoint
        )

        user = s3_conftest.create_user_via_api(
            s3_api_client, s3_instance_uuid, "detach-user", s3_project_id
        )
        policy = s3_conftest.create_policy_via_api(
            s3_api_client,
            s3_instance_uuid,
            "detach-pol",
            _make_readwrite_policy(bucket_name),
            s3_project_id,
        )
        attachment = s3_conftest.attach_policy_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], policy["uuid"], s3_project_id
        )
        key = s3_conftest.create_access_key_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], s3_project_id, s3_endpoint
        )
        client = s3_conftest.make_s3_client(s3_endpoint, key["access_key"], key["secret_key"])

        # Access works with policy attached
        client.put_object(Bucket=bucket_name, Key="obj", Body=b"ok")

        # Detach policy
        policies_collection = (
            f"{s3_conftest.S3_INSTANCES}{s3_instance_uuid}"
            f"/users/{user['uuid']}/policies/"
        )
        s3_api_client.delete(policies_collection, uuid=attachment["uuid"])

        # Wait for dataplane sync
        time.sleep(10)

        # Access should now be denied
        with pytest.raises(botocore.exceptions.ClientError) as exc_info:
            client.put_object(Bucket=bucket_name, Key="obj2", Body=b"nope")
        assert exc_info.value.response["Error"]["Code"] in (
            "AccessDenied",
            "AllAccessDisabled",
        )


class TestPolicyDeletion:
    """Deleting a policy revokes access for all attached users."""

    def test_delete_policy_revokes_access(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_endpoint
    ):
        bucket_name = f"pol-del-{uuid.uuid4().hex[:8]}"
        s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_name, s3_project_id, s3_endpoint
        )

        user = s3_conftest.create_user_via_api(
            s3_api_client, s3_instance_uuid, "pol-del-user", s3_project_id
        )
        policy = s3_conftest.create_policy_via_api(
            s3_api_client,
            s3_instance_uuid,
            "pol-del-pol",
            _make_readwrite_policy(bucket_name),
            s3_project_id,
        )
        s3_conftest.attach_policy_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], policy["uuid"], s3_project_id
        )
        key = s3_conftest.create_access_key_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], s3_project_id, s3_endpoint
        )
        client = s3_conftest.make_s3_client(s3_endpoint, key["access_key"], key["secret_key"])

        # Access works with policy
        client.put_object(Bucket=bucket_name, Key="obj", Body=b"ok")

        # Delete policy
        policies_collection = f"{s3_conftest.S3_INSTANCES}{s3_instance_uuid}/policies/"
        s3_api_client.delete(policies_collection, uuid=policy["uuid"])

        # Wait for dataplane sync
        time.sleep(10)

        # Access should now be denied
        with pytest.raises(botocore.exceptions.ClientError) as exc_info:
            client.put_object(Bucket=bucket_name, Key="obj2", Body=b"nope")
        assert exc_info.value.response["Error"]["Code"] in (
            "AccessDenied",
            "AllAccessDisabled",
        )


class TestMultiplePoliciesPerUser:
    """A user with multiple policies gets cumulative access."""

    def test_cumulative_access(
        self, s3_api_client, s3_instance_uuid, s3_project_id, s3_endpoint
    ):
        bucket_a = f"pol-ma-{uuid.uuid4().hex[:8]}"
        bucket_b = f"pol-mb-{uuid.uuid4().hex[:8]}"
        s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_a, s3_project_id, s3_endpoint
        )
        s3_conftest.create_bucket_via_api(
            s3_api_client, s3_instance_uuid, bucket_b, s3_project_id, s3_endpoint
        )

        user = s3_conftest.create_user_via_api(
            s3_api_client, s3_instance_uuid, "multi-pol-user", s3_project_id
        )
        # Policy A: read-write on bucket_a only
        policy_a = s3_conftest.create_policy_via_api(
            s3_api_client,
            s3_instance_uuid,
            "multi-pol-a",
            _make_readwrite_policy(bucket_a),
            s3_project_id,
        )
        # Policy B: read-write on bucket_b only
        policy_b = s3_conftest.create_policy_via_api(
            s3_api_client,
            s3_instance_uuid,
            "multi-pol-b",
            _make_readwrite_policy(bucket_b),
            s3_project_id,
        )
        s3_conftest.attach_policy_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], policy_a["uuid"], s3_project_id
        )
        s3_conftest.attach_policy_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], policy_b["uuid"], s3_project_id
        )
        key = s3_conftest.create_access_key_via_api(
            s3_api_client, s3_instance_uuid, user["uuid"], s3_project_id, s3_endpoint
        )
        client = s3_conftest.make_s3_client(s3_endpoint, key["access_key"], key["secret_key"])

        # Wait for policies to propagate to the dataplane
        s3_conftest._wait_for_access_key_sync(
            s3_endpoint, key["access_key"], key["secret_key"],
        )

        # User can write to both buckets (cumulative)
        client.put_object(Bucket=bucket_a, Key="obj", Body=b"a")
        client.put_object(Bucket=bucket_b, Key="obj", Body=b"b")
