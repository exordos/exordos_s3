from __future__ import annotations

import logging
import os
import time
import uuid as sys_uuid

from bazooka import exceptions as bazooka_exc
import boto3
import botocore.config
import botocore.exceptions
from exordos.clients import base_client
from gcl_sdk.clients.http import base as http_client
import pytest
import requests

LOG = logging.getLogger(__name__)

# --- Environment configuration ---

EXORDOS_ENDPOINT = os.environ.get("EXORDOS_ENDPOINT", "http://10.20.0.2/api/core")
EXORDOS_USERNAME = os.environ.get("EXORDOS_USERNAME", "admin")
EXORDOS_PASSWORD = os.environ.get("EXORDOS_PASSWORD", "")

# Metapaas project — S3 versions and IAM permissions live here
METAPAAS_PROJECT_ID = os.environ.get(
    "METAPAAS_PROJECT_ID", "4d657461-0000-0000-0000-000000000002"
)

# S3 CP URL — metapaas user-api on metapaas-cp node (port 8080)
# Can be overridden; otherwise resolved from the metapaas-cp compute node.
EXORDOS_S3_CP_URL = os.environ.get("EXORDOS_S3_CP_URL", "")

# S3 data endpoint (RustFS on DP node port 9000); derived from instance if empty
EXORDOS_S3_ENDPOINT = os.environ.get("EXORDOS_S3_ENDPOINT", "")

POLL_TIMEOUT = int(os.environ.get("EXORDOS_POLL_TIMEOUT", "600"))
POLL_INTERVAL = int(os.environ.get("EXORDOS_POLL_INTERVAL", "5"))

# A change accepted by the CP API reaches the dataplane a moment later: a new
# bucket shows up in RustFS, a deleted key stops being accepted.  Poll for the
# effect rather than sleeping a flat ten seconds -- on a quiet runner it lands
# in about a second, and a busy one gets the whole timeout instead of a coin
# flip.
SYNC_TIMEOUT = int(os.environ.get("EXORDOS_SYNC_TIMEOUT", "60"))
SYNC_INTERVAL = 1
# Convergence normally takes about a second, so anything above this is worth
# a line in the log even when the wait does eventually succeed.
SLOW_SYNC = 10

# The core, the metapaas CP and every s3 node share a single CI runner, so the
# core API blips under load: a request comes back 401 because IAM did not
# answer in time, and the token refresh that follows comes back 502.  None of
# that means the instance being polled is unhealthy.
TRANSIENT_API_ERRORS = (bazooka_exc.BaseHTTPException, requests.RequestException)
AUTH_ATTEMPTS = 5
AUTH_RETRY_INTERVAL = 5

S3_INSTANCES = "/v1/types/s3/instances/"
S3_VERSIONS = "/v1/types/s3/versions/"
NODE_COLLECTION = "/v1/compute/nodes/"

OWNER_ROLE_UUID = "726f6c65-0000-0000-0000-000000000002"

IAM_USERS = "/v1/iam/users/"
IAM_ROLE_BINDINGS = "/v1/iam/role_bindings/"


# --- Auth helpers ---


def _get_auth_data(endpoint: str | None = None, project_id: str | None = None) -> dict:
    scope = None
    if project_id:
        scope = http_client.CoreIamAuthenticator.project_scope(
            sys_uuid.UUID(project_id)
        )
    # Omit client_uuid so CoreIamAuthenticator uses its "default" alias and
    # does not send client_id/client_secret — avoids 401 on freshly-bootstrapped cores.
    return {
        "endpoint": endpoint or EXORDOS_ENDPOINT,
        "username": EXORDOS_USERNAME,
        "password": EXORDOS_PASSWORD,
        "access_token": None,
        "refresh_token": None,
        "scope": scope,
    }


class ResilientCoreIamAuthenticator(http_client.CoreIamAuthenticator):
    """A CoreIamAuthenticator that survives a blip in the core IAM API.

    Upstream falls back from the refresh-token grant to the password grant
    only on 400; anything else -- a 502 from an IAM busy reconciling a node --
    propagates out of ``authenticate()`` and poisons the client for the rest
    of the session.  Retry instead: the tests hold a username and a password,
    so a fresh password grant is always available.
    """

    def authenticate(self) -> None:
        for attempt in range(1, AUTH_ATTEMPTS + 1):
            try:
                super().authenticate()
                return
            except TRANSIENT_API_ERRORS as exc:
                if attempt == AUTH_ATTEMPTS:
                    raise
                LOG.warning(
                    "Authentication attempt %s/%s failed: %s",
                    attempt,
                    AUTH_ATTEMPTS,
                    exc,
                )
                # Whatever the cause, the refresh token is the part that can
                # be stale, so drop it and let the retry use the password.
                self._refresh_token = None
                time.sleep(AUTH_RETRY_INTERVAL)


# --- Core client fixture ---


@pytest.fixture(scope="session")
def core_client() -> http_client.CollectionBaseClient:
    return base_client.get_user_api_client(_get_auth_data())


# --- Metapaas CP node resolution ---


@pytest.fixture(scope="session")
def s3_cp_ip(core_client) -> str:
    """Find the IP of the metapaas-cp compute node.

    Returns the IP extracted from EXORDOS_S3_CP_URL if set (avoids a Core API
    call that requires an older auth client format).
    """
    if EXORDOS_S3_CP_URL:
        # Parse IP from e.g. "http://10.20.0.20:8080"
        host = EXORDOS_S3_CP_URL.split("//", 1)[-1].split(":")[0]
        return host

    nodes = core_client.filter(NODE_COLLECTION, name="metapaas-cp")
    if not nodes:
        all_nodes = core_client.filter(NODE_COLLECTION)
        nodes = [n for n in all_nodes if "metapaas" in n.get("name", "").lower()]
    if not nodes:
        pytest.skip(
            "No metapaas-cp compute node found — is metapaas element installed?"
        )
    node = nodes[0]
    net = node.get("default_network", {})
    ip = net.get("ipv4")
    if not ip:
        pytest.skip("metapaas-cp node has no IP yet")
    return ip


# --- Test user and project ---


@pytest.fixture(scope="session")
def test_user(core_client) -> dict:
    test_password = f"S3test{sys_uuid.uuid4().hex[:12]}"
    user_name = f"s3-test-{sys_uuid.uuid4().hex[:8]}"
    user = core_client.create(
        IAM_USERS,
        data={
            "username": user_name,
            "password": test_password,
            "first_name": "S3",
            "last_name": "Tester",
            "email": f"noreply+{user_name}@genesis-core.tech",
        },
    )
    user["password"] = test_password
    yield user
    try:
        core_client.delete(IAM_USERS, uuid=user["uuid"])
    except Exception:
        LOG.exception("Failed to clean up test user %s", user["uuid"])


@pytest.fixture(scope="session")
def test_user_project(core_client, test_user) -> dict:
    """Grant the test user owner role in the metapaas project.

    S3 IAM permissions (s3_instance.create, bucket.*, etc.) are bound to the
    owner role in the metapaas project.  Using a separate test project would
    require re-creating all those bindings; it's simpler to give the test user
    an owner role in the metapaas project for the duration of the test session.
    """
    existing = core_client.filter(
        IAM_ROLE_BINDINGS,
        role=OWNER_ROLE_UUID,
        user=test_user["uuid"],
        project=METAPAAS_PROJECT_ID,
    )
    if not existing:
        core_client.create(
            IAM_ROLE_BINDINGS,
            data={
                "role": f"/v1/iam/roles/{OWNER_ROLE_UUID}",
                "user": f"/v1/iam/users/{test_user['uuid']}",
                "project": f"/v1/iam/projects/{METAPAAS_PROJECT_ID}",
            },
        )
    yield {"uuid": METAPAAS_PROJECT_ID}
    # Role binding cleanup is handled by user deletion in test_user teardown.


# --- S3 CP API client (test user scope) ---


@pytest.fixture(scope="session")
def s3_api_client(
    s3_cp_ip, test_user, test_user_project
) -> http_client.CollectionBaseClient:
    """Authenticated S3 API client on the metapaas-cp node, test user scope."""
    s3_scope = http_client.CoreIamAuthenticator.project_scope(
        sys_uuid.UUID(test_user_project["uuid"])
    )
    core_auth = ResilientCoreIamAuthenticator(
        base_url=EXORDOS_ENDPOINT,
        username=test_user["username"],
        password=test_user["password"],
        scope=s3_scope,
    )
    cp_url = EXORDOS_S3_CP_URL or f"http://{s3_cp_ip}:8080"
    return http_client.CollectionBaseClient(base_url=cp_url, auth=core_auth)


# --- S3 version (from the metapaas project) ---


@pytest.fixture(scope="session")
def s3_version_uuid(s3_api_client) -> str:
    """Get the first available S3 version UUID from the metapaas project.

    The versions live in the metapaas project, which is the project the test
    user is scoped to and owns — the same owner role that carries every other
    s3 permission also carries s3_version_read, so no separate service
    account (and no reading of its generated password) is needed.
    """
    versions = s3_api_client.filter(S3_VERSIONS)
    if not versions:
        pytest.skip("No S3 versions registered — is s3aas element installed?")
    return versions[0]["uuid"]


# --- S3 instance ---


@pytest.fixture(scope="session")
def s3_instance(s3_api_client, s3_version_uuid, test_user_project) -> dict:
    """Create an S3 instance and wait until ACTIVE."""
    instance_name = f"test-int-{sys_uuid.uuid4().hex[:8]}"
    # 'kind' is a read-only field (defaults to single_node); sending it on
    # create triggers a FieldPermissionError, so it is intentionally omitted.
    data = {
        "name": instance_name,
        "project_id": test_user_project["uuid"],
        "cpu": 1,
        "ram": 1024,
        "disk_size": 10,
        "nodes_number": 1,
        "version": f"{S3_VERSIONS}{s3_version_uuid}",
    }
    instance = s3_api_client.create(S3_INSTANCES, data=data)
    instance_uuid = instance["uuid"]
    LOG.info(
        "Created s3 instance %s (%s), waiting for ACTIVE", instance_name, instance_uuid
    )
    yield _poll_instance_status(
        s3_api_client, instance_uuid, "ACTIVE", POLL_TIMEOUT, POLL_INTERVAL
    )
    try:
        s3_api_client.delete(S3_INSTANCES, uuid=instance_uuid)
    except Exception:
        LOG.exception("Failed to clean up S3 instance %s", instance_uuid)


def _poll_instance_status(client, instance_uuid, target_status, timeout, interval):
    start = last_report = time.monotonic()
    deadline = start + timeout
    last_status = ""
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            instance = client.get(S3_INSTANCES, uuid=instance_uuid)
        except TRANSIENT_API_ERRORS as exc:
            # This fixture is session-scoped: one unlucky request would fail
            # every test in the run.  Keep polling until the deadline and
            # report the error only if the API never comes back.
            last_error = exc
            LOG.warning("Polling instance %s failed: %s", instance_uuid, exc)
            time.sleep(interval)
            continue
        last_error = None
        status = instance.get("status", "")
        elapsed = round(time.monotonic() - start)
        # A status change is news; otherwise say something every half minute
        # so a run that takes minutes does not look like a hung one.
        if status != last_status or time.monotonic() - last_report >= 30:
            LOG.info("Instance %s is %s after %ds", instance_uuid, status, elapsed)
            last_report = time.monotonic()
        last_status = status
        if last_status == target_status:
            LOG.info(
                "Instance %s reached %s after %ds",
                instance_uuid,
                target_status,
                elapsed,
            )
            return instance
        if last_status in ("ERROR", "CREATE_FAILED", "DELETE_FAILED"):
            pytest.fail(f"Instance entered terminal status: {last_status}")
        time.sleep(interval)
    detail = f"last status: {last_status or 'unknown'}"
    if last_error is not None:
        detail += f", last error: {last_error}"
    pytest.fail(
        f"Instance {instance_uuid} did not reach {target_status} "
        f"within {timeout}s ({detail})"
    )


# --- Derived fixtures ---


@pytest.fixture(scope="session")
def s3_instance_uuid(s3_instance) -> str:
    return s3_instance["uuid"]


@pytest.fixture(scope="session")
def s3_project_id(test_user_project) -> str:
    return test_user_project["uuid"]


@pytest.fixture(scope="session")
def s3_endpoint(s3_instance) -> str:
    if EXORDOS_S3_ENDPOINT:
        return EXORDOS_S3_ENDPOINT
    ips = s3_instance.get("ipsv4", [])
    if not ips:
        pytest.skip("Instance has no IPs, set EXORDOS_S3_ENDPOINT explicitly")
    return f"{ips[0]}:9000"


@pytest.fixture(scope="session")
def s3_buckets(s3_api_client, s3_instance_uuid) -> list[dict]:
    collection = f"{S3_INSTANCES}{s3_instance_uuid}/buckets/"
    return s3_api_client.filter(collection)


@pytest.fixture(scope="session")
def s3_users(s3_api_client, s3_instance_uuid, s3_project_id) -> list[dict]:
    collection = f"{S3_INSTANCES}{s3_instance_uuid}/users/"
    session_user = s3_api_client.create(
        collection,
        data={
            "name": f"session-user-{sys_uuid.uuid4().hex[:8]}",
            "project_id": s3_project_id,
            "instance": f"{S3_INSTANCES}{s3_instance_uuid}",
        },
    )
    policy_collection = f"{S3_INSTANCES}{s3_instance_uuid}/policies/"
    policy = s3_api_client.create(
        policy_collection,
        data={
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
        },
    )
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
    collection = f"{S3_INSTANCES}{s3_instance_uuid}/policies/"
    return s3_api_client.filter(collection)


@pytest.fixture(scope="session")
def access_keys_with_secrets(
    s3_api_client, s3_instance, s3_instance_uuid, s3_users, s3_endpoint
) -> dict:
    result = {}
    for user in s3_users:
        user_uuid = user["uuid"]
        user_name = user["name"]
        keys_collection = f"{S3_INSTANCES}{s3_instance_uuid}/users/{user_uuid}/keys/"
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
        _wait_for_access_key_sync(
            s3_endpoint, key_resp["access_key"], key_resp["secret_key"]
        )
    return result


@pytest.fixture(scope="session")
def s3_clients(access_keys_with_secrets, s3_endpoint) -> dict[str, boto3.client]:
    clients = {}
    for user_name, keys in access_keys_with_secrets.items():
        key = keys[0]
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


@pytest.fixture(scope="session")
def s3_probe_client(s3_clients) -> boto3.client:
    """The session user's client, used to watch the dataplane catch up.

    Its policy allows everything on every bucket, so it can see whatever the
    tests create -- including buckets that belong to a user the test has not
    built yet.
    """
    return next(iter(s3_clients.values()))


# --- Helper utilities ---


def upload_test_object(s3_client, bucket, key, content=None) -> str:
    if content is None:
        content = f"test-content-{time.time()}"
    if isinstance(content, str):
        content = content.encode()
    s3_client.put_object(Bucket=bucket, Key=key, Body=content)
    return key


def download_object(s3_client, bucket, key) -> bytes:
    resp = s3_client.get_object(Bucket=bucket, Key=key)
    return resp["Body"].read()


def _generate_test_secret_key() -> str:
    return "x" * 64


def _wait_for_access_key_sync(
    s3_endpoint, access_key, secret_key, timeout=120, interval=SYNC_INTERVAL
):
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
            return
        except botocore.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in (
                "InvalidAccessKeyId",
                "SignatureDoesNotMatch",
                "AccessDenied",
                "AllAccessDisabled",
                "",
            ):
                time.sleep(interval)
                continue
            raise
        except botocore.exceptions.EndpointConnectionError:
            time.sleep(interval)
            continue
    raise TimeoutError(f"Access key {access_key} not synced within {timeout}s")


def wait_for_bucket(
    s3_client, bucket_name, present=True, timeout=SYNC_TIMEOUT, interval=SYNC_INTERVAL
):
    """Wait until the bucket does (or no longer does) exist on the dataplane."""
    start = time.monotonic()
    deadline = start + timeout
    last_error = ""
    while True:
        try:
            s3_client.head_bucket(Bucket=bucket_name)
            found, last_error = True, ""
        except botocore.exceptions.ClientError as e:
            # A bucket that is not there yet answers 404; a key the dataplane
            # has not learned about yet answers 403 or InvalidAccessKeyId.
            # Either way it is not serving this bucket.
            last_error = e.response.get("Error", {}).get("Code", "")
            found = False
        except botocore.exceptions.EndpointConnectionError as e:
            last_error, found = str(e), False
        if found == present:
            if time.monotonic() - start > SLOW_SYNC:
                LOG.warning(
                    "Bucket %s took %ds to %s on the dataplane",
                    bucket_name,
                    round(time.monotonic() - start),
                    "appear" if present else "disappear",
                )
            return
        if time.monotonic() >= deadline:
            state = "appear" if present else "disappear"
            detail = f" (last error: {last_error})" if last_error else ""
            raise TimeoutError(
                f"Bucket {bucket_name} did not {state} within {timeout}s{detail}"
            )
        time.sleep(interval)


def wait_until_denied(operation, timeout=SYNC_TIMEOUT, interval=SYNC_INTERVAL):
    """Wait until an S3 operation starts failing, and return the error.

    A revocation -- a deleted key, a detached policy -- reaches the dataplane
    after the CP API has confirmed it, so the call keeps working for a moment.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            operation()
        except botocore.exceptions.ClientError as e:
            return e
        if time.monotonic() >= deadline:
            pytest.fail(f"Operation was still permitted after {timeout}s")
        time.sleep(interval)


def wait_until_allowed(operation, timeout=SYNC_TIMEOUT, interval=SYNC_INTERVAL):
    """Wait until an S3 operation starts succeeding, and return its result."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return operation()
        except botocore.exceptions.ClientError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(interval)


def make_s3_client(s3_endpoint, access_key, secret_key):
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
    s3_api_client, instance_uuid, name, project_id, s3_client=None, **kwargs
):
    """Create a bucket via the CP API.

    Pass the client the test is about to use and this returns only once the
    dataplane serves the bucket.  Tests that never leave the CP API -- the
    read-only field ones -- omit it and do not wait for a dataplane they are
    not going to talk to.
    """
    collection = f"{S3_INSTANCES}{instance_uuid}/buckets/"
    data = {
        "name": name,
        "project_id": project_id,
        "instance": f"{S3_INSTANCES}{instance_uuid}",
        **kwargs,
    }
    result = s3_api_client.create(collection, data=data)
    if s3_client is not None:
        wait_for_bucket(s3_client, name)
    return result


def create_user_via_api(s3_api_client, instance_uuid, name, project_id):
    collection = f"{S3_INSTANCES}{instance_uuid}/users/"
    data = {
        "name": name,
        "project_id": project_id,
        "instance": f"{S3_INSTANCES}{instance_uuid}",
    }
    return s3_api_client.create(collection, data=data)


def create_policy_via_api(
    s3_api_client, instance_uuid, name, content, project_id, **kwargs
):
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
    s3_api_client, instance_uuid, user_uuid, policy_uuid, project_id
):
    collection = f"{S3_INSTANCES}{instance_uuid}/users/{user_uuid}/policies/"
    data = {
        "project_id": project_id,
        "instance": f"{S3_INSTANCES}{instance_uuid}",
        "policy": f"{S3_INSTANCES}{instance_uuid}/policies/{policy_uuid}",
        "user": f"{S3_INSTANCES}{instance_uuid}/users/{user_uuid}",
    }
    return s3_api_client.create(collection, data=data)


def create_access_key_via_api(
    s3_api_client, instance_uuid, user_uuid, project_id, s3_endpoint
):
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
    _wait_for_access_key_sync(s3_endpoint, result["access_key"], result["secret_key"])
    return result
