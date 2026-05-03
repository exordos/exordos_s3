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
from __future__ import annotations

import os
import time
import typing as tp
import uuid as sys_uuid

import boto3
import botocore.config
import botocore.exceptions
import pytest
from gcl_iam.tests.functional import clients as iam_clients
from gcl_sdk.clients.http import base as http_client

from genesis_devtools.clients import base_client


# --- Environment configuration ---

EXORDOS_ENDPOINT = os.environ.get("EXORDOS_ENDPOINT", "http://10.20.0.2:11010")
EXORDOS_USERNAME = os.environ.get("EXORDOS_USERNAME", "admin")
EXORDOS_PASSWORD = os.environ.get("EXORDOS_PASSWORD", "")
EXORDOS_PROJECT_ID = os.environ.get("EXORDOS_PROJECT_ID", "")

# S3aaS CP API endpoint — resolved from DNS or set explicitly
# Default: s3aas-cp.local.genesis-core.tech:8080
EXORDOS_S3_CP_URL = os.environ.get(
    "EXORDOS_S3_CP_URL", "http://s3aas-cp.local.genesis-core.tech:8080"
)

# S3 data endpoint (RustFS on DP node port 9000)
# If empty, derived from instance.ipsv4
EXORDOS_S3_ENDPOINT = os.environ.get("EXORDOS_S3_ENDPOINT", "")

# Polling defaults — CP/DP VMs take time to boot
POLL_TIMEOUT = int(os.environ.get("EXORDOS_POLL_TIMEOUT", "600"))
POLL_INTERVAL = int(os.environ.get("EXORDOS_POLL_INTERVAL", "15"))

# S3 API collection paths (relative to S3 CP API)
S3_INSTANCES = "/v1/types/s3/instances/"
S3_VERSIONS = "/v1/types/s3/versions/"
NODE_COLLECTION = "/v1/compute/nodes/"

# IAM constants
OWNER_ROLE_UUID = "726f6c65-0000-0000-0000-000000000002"
DEFAULT_CLIENT_UUID = "00000000-0000-0000-0000-000000000000"
DEFAULT_CLIENT_ID = "GenesisCoreClientId"
DEFAULT_CLIENT_SECRET = "GenesisCoreSecret"


# --- Auth helpers ---


def _get_auth_data(endpoint: str | None = None) -> dict[str, tp.Any]:
    scope = None
    if EXORDOS_PROJECT_ID:
        scope = http_client.CoreIamAuthenticator.project_scope(
            sys_uuid.UUID(EXORDOS_PROJECT_ID)
        )
    return dict(
        endpoint=endpoint or EXORDOS_ENDPOINT,
        username=EXORDOS_USERNAME,
        password=EXORDOS_PASSWORD,
        access_token=None,
        refresh_token=None,
        scope=scope,
    )


# --- Core client fixture ---


@pytest.fixture(scope="session")
def core_client() -> http_client.CollectionBaseClient:
    """Authenticated genesis_core API client (no project scope).

    Works for compute nodes and other non-project-scoped resources.
    """
    return base_client.get_user_api_client(_get_auth_data())


@pytest.fixture(scope="session")
def iam_rest_client() -> iam_clients.GenericAutoRefreshRESTClient:
    """IAM REST client for creating projects, users, role bindings.

    Uses gcl_iam test client which correctly formats URI references
    for organization, user, role fields in IAM API requests.
    """
    auth = iam_clients.GenesisCoreAuth(
        username=EXORDOS_USERNAME,
        password=EXORDOS_PASSWORD,
        client_uuid=DEFAULT_CLIENT_UUID,
        client_id=DEFAULT_CLIENT_ID,
        client_secret=DEFAULT_CLIENT_SECRET,
    )
    endpoint = f"{EXORDOS_ENDPOINT.rstrip('/')}/v1/"
    return iam_clients.GenericAutoRefreshRESTClient(endpoint, auth)


# --- S3aaS CP API client ---


@pytest.fixture(scope="session")
def s3_cp_ip(core_client) -> str:
    """Find the IP of the s3aas-cp compute node via Core API."""
    nodes = core_client.filter(NODE_COLLECTION, name="s3aas-cp")
    if not nodes:
        pytest.skip("No s3aas-cp compute node found — is s3aas element installed?")
    node = nodes[0]
    net = node.get("default_network", {})
    ip = net.get("ipv4")
    if not ip:
        pytest.skip("s3aas-cp node has no IP yet")
    return ip


@pytest.fixture(scope="session")
def test_user(iam_rest_client) -> dict:
    """Create a dedicated test user for S3 functional tests (session-scoped).

    Returns dict with 'username', 'password', 'uuid'.
    """
    test_password = f"S3test{sys_uuid.uuid4().hex[:12]}"
    user_name = f"s3-test-{sys_uuid.uuid4().hex[:8]}"
    user = iam_rest_client.create_user(
        username=user_name,
        password=test_password,
        first_name="S3",
        last_name="Tester",
        email=f"noreply+{user_name}@genesis-core.tech",
    )

    # Store password in the dict for auth (not returned by API)
    user["password"] = test_password
    yield user

    # Teardown: delete user
    try:
        iam_rest_client.delete_user(user["uuid"])
    except Exception:
        pass


@pytest.fixture(scope="session")
def test_user_project(iam_rest_client, test_user) -> dict:
    """Create a dedicated test project and grant owner role to the test user.

    Also binds all exordos_s3 permissions to owner role in this project.
    """
    org_name = f"s3-test-org-{sys_uuid.uuid4().hex[:8]}"
    org = iam_rest_client.create_organization(name=org_name)

    project_name = f"s3-test-project-{sys_uuid.uuid4().hex[:8]}"
    project = iam_rest_client.create_project(
        organization_uuid=org["uuid"],
        name=project_name,
        description="S3aaS functional test project",
    )

    iam_rest_client.create_or_get_role_binding(
        role_uuid=OWNER_ROLE_UUID,
        user_uuid=test_user["uuid"],
        project_id=project["uuid"],
    )

    yield project

    try:
        iam_rest_client.delete_project(project["uuid"])
    except Exception:
        pass
    try:
        iam_rest_client.delete_organization(org["uuid"])
    except Exception:
        pass


@pytest.fixture(scope="session")
def s3_api_client(
    s3_cp_ip, test_user, test_user_project
) -> http_client.CollectionBaseClient:
    """Authenticated client for S3aaS user API on CP node.

    Authenticates as the test user (via Core IAM) with the dedicated test project
    scope, then sends API requests to the S3aaS CP node.
    """
    s3_scope = http_client.CoreIamAuthenticator.project_scope(
        sys_uuid.UUID(test_user_project["uuid"])
    )
    core_auth = http_client.CoreIamAuthenticator(
        base_url=EXORDOS_ENDPOINT,
        username=test_user["username"],
        password=test_user["password"],
        scope=s3_scope,
    )
    cp_url = f"http://{s3_cp_ip}:8080"
    return http_client.CollectionBaseClient(
        base_url=cp_url,
        auth=core_auth,
    )


# --- S3aaS element + instance deployment (session-scoped) ---


@pytest.fixture(scope="session")
def s3_version_uuid(s3_api_client) -> str:
    """Get the first available S3 version UUID from S3aaS CP API."""
    versions = s3_api_client.filter(S3_VERSIONS)
    if not versions:
        pytest.skip("No S3 versions registered — is s3aas element installed?")
    return versions[0]["uuid"]


@pytest.fixture(scope="session")
def s3_instance(s3_api_client, s3_version_uuid, test_user_project) -> dict:
    """Create an S3 instance and wait until ACTIVE.

    Deploys once per session. Cleanup: delete on teardown.
    """
    instance_name = f"test-int-{sys_uuid.uuid4().hex[:8]}"
    data = {
        "name": instance_name,
        "project_id": test_user_project["uuid"],
        "cpu": 1,
        "ram": 2048,
        "disk_size": 10,
        "nodes_number": 1,
        "version": f"{S3_VERSIONS}{s3_version_uuid}",
    }
    instance = s3_api_client.create(S3_INSTANCES, data=data)
    instance_uuid = instance["uuid"]

    yield _poll_instance_status(
        s3_api_client, instance_uuid, "ACTIVE", POLL_TIMEOUT, POLL_INTERVAL
    )

    # Teardown: delete instance
    try:
        s3_api_client.delete(S3_INSTANCES, uuid=instance_uuid)
    except Exception:
        pass


def _poll_instance_status(
    client: http_client.CollectionBaseClient,
    instance_uuid: str,
    target_status: str,
    timeout: int,
    interval: int,
) -> dict:
    """Poll instance status until it reaches target_status or timeout."""
    deadline = time.monotonic() + timeout
    last_status = ""
    while time.monotonic() < deadline:
        instance = client.get(S3_INSTANCES, uuid=instance_uuid)
        last_status = instance.get("status", "")
        if last_status == target_status:
            return instance
        if last_status in ("ERROR", "CREATE_FAILED", "DELETE_FAILED"):
            pytest.fail(f"Instance entered terminal status: {last_status}")
        time.sleep(interval)
    pytest.fail(
        f"Instance {instance_uuid} did not reach {target_status} "
        f"within {timeout}s (last status: {last_status})"
    )


# --- Derived fixtures ---


@pytest.fixture(scope="session")
def s3_instance_uuid(s3_instance) -> str:
    return s3_instance["uuid"]


@pytest.fixture(scope="session")
def s3_project_id(test_user_project) -> str:
    """Project ID the S3 instance belongs to."""
    return test_user_project["uuid"]


@pytest.fixture(scope="session")
def s3_endpoint(s3_instance) -> str:
    """RustFS S3 endpoint (host:port) for data operations.

    Priority:
    1. EXORDOS_S3_ENDPOINT env var (explicit override)
    2. instance.ipsv4[0]:9000 (from CP API)
    """
    if EXORDOS_S3_ENDPOINT:
        return EXORDOS_S3_ENDPOINT
    ips = s3_instance.get("ipsv4", [])
    if not ips:
        pytest.skip("Instance has no IPs, set EXORDOS_S3_ENDPOINT explicitly")
    return f"{ips[0]}:9000"


@pytest.fixture(scope="session")
def s3_buckets(s3_api_client, s3_instance_uuid) -> list[dict]:
    """List buckets for the instance."""
    collection = f"{S3_INSTANCES}{s3_instance_uuid}/buckets/"
    return s3_api_client.filter(collection)


@pytest.fixture(scope="session")
def s3_users(s3_api_client, s3_instance_uuid, s3_project_id) -> list[dict]:
    """Create a deterministic session user with read-write policy for s3_clients fixture."""
    collection = f"{S3_INSTANCES}{s3_instance_uuid}/users/"
    session_user = s3_api_client.create(
        collection,
        data={
            "name": f"session-user-{sys_uuid.uuid4().hex[:8]}",
            "project_id": s3_project_id,
            "instance": f"{S3_INSTANCES}{s3_instance_uuid}",
        },
    )

    # Create a read-write policy for the session user
    policy_collection = f"{S3_INSTANCES}{s3_instance_uuid}/policies/"
    policy_data = {
        "project_id": s3_project_id,
        "instance": f"{S3_INSTANCES}{s3_instance_uuid}",
        "name": "session-user-rw",
        "content": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:*"],
                    "Resource": ["arn:aws:s3:::*", "arn:aws:s3:::*/*"],
                }
            ],
        },
    }
    policy = s3_api_client.create(policy_collection, data=policy_data)

    # Attach policy to user
    attachment_collection = (
        f"{S3_INSTANCES}{s3_instance_uuid}/users/{session_user['uuid']}/policies/"
    )
    s3_api_client.create(
        attachment_collection,
        data={
            "project_id": s3_project_id,
            "instance": f"{S3_INSTANCES}{s3_instance_uuid}",
            "policy": f"{S3_INSTANCES}{s3_instance_uuid}/policies/{policy['uuid']}",
            "user": f"{S3_INSTANCES}{s3_instance_uuid}/users/{session_user['uuid']}",
        },
    )

    return [session_user]


@pytest.fixture(scope="session")
def s3_policies(s3_api_client, s3_instance_uuid) -> list[dict]:
    """List policies for the instance."""
    collection = f"{S3_INSTANCES}{s3_instance_uuid}/policies/"
    return s3_api_client.filter(collection)


@pytest.fixture(scope="session")
def access_keys_with_secrets(
    s3_api_client, s3_instance, s3_instance_uuid, s3_users, s3_endpoint
) -> dict:
    """Access keys with secret_key per user: {user_name: [{access_key, secret_key, uuid}]}.

    secret_key is only visible at creation time, so we create new keys
    for each user and capture the response.

    Waits for each key to be synced to the dataplane before returning.
    """
    result = {}
    for user in s3_users:
        user_uuid = user["uuid"]
        user_name = user["name"]
        keys_collection = f"{S3_INSTANCES}{s3_instance_uuid}/users/{user_uuid}/keys/"

        # Create a new access key to capture secret_key
        secret_key = _generate_test_secret_key()
        key_data = {
            "project_id": s3_instance.get("project_id", ""),
            "instance": f"{S3_INSTANCES}{s3_instance_uuid}",
            "user": f"{S3_INSTANCES}{s3_instance_uuid}/users/{user_uuid}",
            "secret_key": secret_key,
        }
        key_resp = s3_api_client.create(keys_collection, data=key_data)
        key_resp["secret_key"] = key_resp.get("secret_key", secret_key)
        result.setdefault(user_name, []).append(
            {
                "access_key": key_resp["access_key"],
                "secret_key": key_resp["secret_key"],
                "uuid": key_resp["uuid"],
            }
        )

        # Wait for the key to be synced to RustFS dataplane
        _wait_for_access_key_sync(
            s3_endpoint, key_resp["access_key"], key_resp["secret_key"]
        )
    return result


@pytest.fixture(scope="session")
def s3_clients(access_keys_with_secrets, s3_endpoint) -> dict[str, boto3.client]:
    """boto3 S3 client per user name, ready for data operations."""
    clients = {}
    for user_name, keys in access_keys_with_secrets.items():
        key = keys[0]  # Use first key per user
        client = boto3.client(
            "s3",
            endpoint_url=f"http://{s3_endpoint}",
            aws_access_key_id=key["access_key"],
            aws_secret_access_key=key["secret_key"],
            config=botocore.config.Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
            ),
            region_name="us-east-1",
        )
        clients[user_name] = client
    return clients


# --- Helper utilities for test modules ---


def upload_test_object(
    s3_client, bucket: str, key: str, content: str | bytes | None = None
) -> str:
    """Upload a small test object, return the key."""
    if content is None:
        content = f"test-content-{time.time()}"
    if isinstance(content, str):
        content = content.encode()
    s3_client.put_object(Bucket=bucket, Key=key, Body=content)
    return key


def download_object(s3_client, bucket: str, key: str) -> bytes:
    """Download object body as bytes."""
    resp = s3_client.get_object(Bucket=bucket, Key=key)
    return resp["Body"].read()


def _generate_test_secret_key() -> str:
    """Generate a deterministic secret key for testing."""
    return "x" * 64


def _wait_for_access_key_sync(
    s3_endpoint: str,
    access_key: str,
    secret_key: str,
    timeout: int = 120,
    interval: int = 3,
) -> None:
    """Wait for access key to be synced to RustFS dataplane.

    Polls the S3 endpoint with the new credentials until list_buckets
    succeeds (meaning the key exists AND the user has s3:ListAllMyBuckets)
    or timeout is reached.

    For users without s3:ListAllMyBuckets, this will wait until timeout
    and then raise TimeoutError — callers should add a fixed sleep as
    a fallback for such cases.
    """
    client = boto3.client(
        "s3",
        endpoint_url=f"http://{s3_endpoint}",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=botocore.config.Config(
            signature_version="s3v4",
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )
    start = time.time()
    while time.time() - start < timeout:
        try:
            client.list_buckets()
            return  # Success — key is synced and user has list permission
        except botocore.exceptions.ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in (
                "InvalidAccessKeyId",
                "SignatureDoesNotMatch",
                "AccessDenied",
                "AllAccessDisabled",
                "",
            ):
                time.sleep(interval)
                continue
            raise  # Other error
        except botocore.exceptions.EndpointConnectionError:
            time.sleep(interval)
            continue
    raise TimeoutError(
        f"Access key {access_key} not synced to RustFS within {timeout}s"
    )


def _wait_for_bucket_sync(
    s3_endpoint: str,
    bucket_name: str,
    access_key: str,
    secret_key: str,
    timeout: int = 60,
    interval: int = 2,
) -> None:
    """Wait for bucket to be synced to RustFS dataplane.

    Polls the S3 endpoint with provided credentials until the bucket exists.
    """
    client = boto3.client(
        "s3",
        endpoint_url=f"http://{s3_endpoint}",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=botocore.config.Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )
    start = time.time()
    while time.time() - start < timeout:
        try:
            client.head_bucket(Bucket=bucket_name)
            return  # Success
        except botocore.exceptions.ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("NoSuchBucket", "404", "InvalidAccessKeyId", ""):
                time.sleep(interval)
                continue
            raise  # Other error
        except botocore.exceptions.EndpointConnectionError:
            time.sleep(interval)
            continue
    raise TimeoutError(f"Bucket {bucket_name} not synced to RustFS within {timeout}s")


def make_s3_client(s3_endpoint: str, access_key: str, secret_key: str):
    """Create a boto3 S3 client with given credentials."""
    return boto3.client(
        "s3",
        endpoint_url=f"http://{s3_endpoint}",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=botocore.config.Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
        ),
        region_name="us-east-1",
    )


def create_bucket_via_api(
    s3_api_client,
    instance_uuid: str,
    name: str,
    project_id: str,
    s3_endpoint: str,
    **kwargs,
) -> dict:
    """Create a bucket via S3 CP API.

    Waits briefly for the bucket to be synced to the dataplane.
    """
    collection = f"{S3_INSTANCES}{instance_uuid}/buckets/"
    data = {
        "name": name,
        "project_id": project_id,
        "instance": f"{S3_INSTANCES}{instance_uuid}",
        **kwargs,
    }
    result = s3_api_client.create(collection, data=data)

    # Wait for dataplane sync (simple sleep - agent polling interval is ~5s)
    time.sleep(10)
    return result


def create_user_via_api(
    s3_api_client, instance_uuid: str, name: str, project_id: str
) -> dict:
    """Create a user via S3 CP API."""
    collection = f"{S3_INSTANCES}{instance_uuid}/users/"
    data = {
        "name": name,
        "project_id": project_id,
        "instance": f"{S3_INSTANCES}{instance_uuid}",
    }
    return s3_api_client.create(collection, data=data)


def create_policy_via_api(
    s3_api_client, instance_uuid: str, name: str, content: dict, project_id: str,
    **kwargs,
) -> dict:
    """Create a policy via S3 CP API."""
    collection = f"{S3_INSTANCES}{instance_uuid}/policies/"
    data = {
        "name": name,
        "project_id": project_id,
        "instance": f"{S3_INSTANCES}{instance_uuid}",
        "content": content,
        **kwargs,
    }
    return s3_api_client.create(collection, data=data)


def attach_policy_via_api(
    s3_api_client,
    instance_uuid: str,
    user_uuid: str,
    policy_uuid: str,
    project_id: str,
) -> dict:
    """Attach a policy to a user via S3 CP API."""
    collection = f"{S3_INSTANCES}{instance_uuid}/users/{user_uuid}/policies/"
    data = {
        "project_id": project_id,
        "instance": f"{S3_INSTANCES}{instance_uuid}",
        "policy": f"{S3_INSTANCES}{instance_uuid}/policies/{policy_uuid}",
        "user": f"{S3_INSTANCES}{instance_uuid}/users/{user_uuid}",
    }
    return s3_api_client.create(collection, data=data)


def create_access_key_via_api(
    s3_api_client,
    instance_uuid: str,
    user_uuid: str,
    project_id: str,
    s3_endpoint: str,
) -> dict:
    """Create an access key via S3 CP API, returns dict with secret_key.

    Waits for the key to be synced to the dataplane before returning.
    Also waits for attached policies to propagate to RustFS.
    """
    collection = f"{S3_INSTANCES}{instance_uuid}/users/{user_uuid}/keys/"
    secret_key = _generate_test_secret_key()
    data = {
        "project_id": project_id,
        "instance": f"{S3_INSTANCES}{instance_uuid}",
        "user": f"{S3_INSTANCES}{instance_uuid}/users/{user_uuid}",
        "secret_key": secret_key,
    }
    result = s3_api_client.create(collection, data=data)
    result["secret_key"] = result.get("secret_key", secret_key)

    # Wait for the key to be synced to RustFS dataplane.
    # If the user lacks s3:ListAllMyBuckets, list_buckets will keep
    # returning AccessDenied — in that case, fall back to a fixed sleep.
    try:
        _wait_for_access_key_sync(
            s3_endpoint, result["access_key"], result["secret_key"],
        )
    except TimeoutError:
        # Key likely synced but user has no list permission —
        # give the agent extra time to apply policies
        time.sleep(10)

    return result
