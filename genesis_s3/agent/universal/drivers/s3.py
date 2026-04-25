#    Copyright 2025 Genesis Corporation.
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

import datetime
import hashlib
import json
import logging
import os
import urllib.parse

from gcl_sdk.agents.universal import constants as c
from gcl_sdk.agents.universal.drivers import meta
from gcl_sdk.infra import constants as pc
from minio import Minio
from minio import credentials as minio_creds
from minio import error as minio_error
from minio import objectlockconfig as minio_olc
from minio import retention as minio_retention
from minio import signer as minio_signer
from minio import versioningconfig as minio_vc
import requests
from restalchemy.common import singletons
from restalchemy.dm import properties
from restalchemy.dm import types as ra_types

from genesis_s3.common import constants

LOG = logging.getLogger(__name__)

SYSTEM_POLICIES = {
    "consoleAdmin",
    "diagnostics",
    "readonly",
    "readwrite",
    "writeonly",
}


def _normalize_actual_policy(policy):
    """Normalize actual policy returned by RustFS for comparison.

    RustFS adds empty ``ID``, ``Sid`` and ``Condition`` fields when storing.
    Strip those and sort list fields so that semantically identical policies
    compare equal to the target.  Mutates the input — acceptable since the
    value is only used for comparison.
    """
    if isinstance(policy, str):
        try:
            policy = json.loads(policy)
        except (json.JSONDecodeError, TypeError):
            return policy

    if not isinstance(policy, dict):
        return policy

    policy.pop("ID", None)

    for stmt in policy.get("Statement", []):
        if not isinstance(stmt, dict):
            continue
        stmt.pop("Condition", None)
        stmt.pop("Sid", None)
        for key in ("Action", "Resource"):
            val = stmt.get(key)
            if isinstance(val, list):
                stmt[key] = sorted(val)
            elif isinstance(val, str):
                stmt[key] = [val]

    return policy


def _policy_content_equal(target, actual):
    """Compare target and actual policy content semantically."""
    a = _normalize_actual_policy(actual)
    t = _normalize_actual_policy(target)
    return json.dumps(t, sort_keys=True) == json.dumps(a, sort_keys=True)


class AdminClient(singletons.InheritSingleton):
    """Singleton client for managing local rustfs instance.

    Reads root credentials from the local rustfs env file,
    which is delivered by the infra Config resource.
    """

    def __init__(self):
        self._load_config()
        self._client = Minio(
            self._endpoint,
            access_key=self._root_user,
            secret_key=self._root_password,
            secure=False,
        )
        self._admin_base_url = f"http://{self._endpoint}/rustfs/admin/v3"

    @staticmethod
    def _parse_env_file(path):
        env = {}
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
        return env

    def _load_config(self):
        env = self._parse_env_file(constants.RUSTFS_ENV_FILE)
        self._root_user = env.get("RUSTFS_ACCESS_KEY", "admin")
        self._root_password = env["RUSTFS_SECRET_KEY"]
        self._endpoint = env.get("RUSTFS_ADDRESS", "127.0.0.1:9000")

    @property
    def client(self) -> Minio:
        return self._client

    # -- Admin HTTP API helpers --
    # RustFS admin API is available at /rustfs/admin/v3
    # (MinIO uses /mapi/v1/, but RustFS has its own API)
    # We use requests directly since minio-py doesn't expose all admin ops.

    def _build_signed_admin_request(
        self, method, path, access_key, secret_key, json_data=None, body=None
    ):
        """Build and send a signed admin API request using the given credentials."""
        url_str = f"{self._admin_base_url}{path}"
        url = requests.utils.urlparse(url_str)

        # Prepare body and content hash
        if body is not None:
            body_bytes = body
            content_type = "application/json"
        elif json_data is not None:
            body_bytes = json.dumps(json_data).encode("utf-8")
            content_type = "application/json"
        else:
            body_bytes = b""
            content_type = None
        content_sha256 = hashlib.sha256(body_bytes).hexdigest()

        # Prepare headers
        date = datetime.datetime.now(datetime.timezone.utc)
        amz_date = date.strftime("%Y%m%dT%H%M%SZ")
        headers = {
            "Host": url.netloc,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": content_sha256,
        }
        if content_type:
            headers["Content-Type"] = content_type

        # Sign with the provided credentials
        creds = minio_creds.Credentials(
            access_key=access_key,
            secret_key=secret_key,
        )
        signed_headers = minio_signer.sign_v4_s3(
            method=method,
            url=url,
            region="us-east-1",
            headers=headers,
            credentials=creds,
            content_sha256=content_sha256,
            date=date,
        )
        headers.update(signed_headers)

        resp = requests.request(
            method,
            url_str,
            data=body_bytes if body_bytes else None,
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        return resp

    def _admin_request(self, method, path, json_data=None, body=None):
        """Admin request signed with root credentials."""
        return self._build_signed_admin_request(
            method,
            path,
            self._root_user,
            self._root_password,
            json_data=json_data,
            body=body,
        )

    # -- Bucket operations --

    def list_buckets(self):
        return {b.name: b for b in self._client.list_buckets()}

    def make_bucket(self, name, object_lock=False):
        if not self._client.bucket_exists(name):
            self._client.make_bucket(name, object_lock=object_lock)
            LOG.info("Bucket %s created (object_lock=%s)", name, object_lock)
        else:
            LOG.debug("Bucket %s already exists", name)

    def remove_bucket(self, name):
        if self._client.bucket_exists(name):
            self._client.remove_bucket(name)
            LOG.info("Bucket %s removed", name)

    def set_bucket_versioning(self, name, enabled):
        status = minio_vc.ENABLED if enabled else minio_vc.SUSPENDED
        self._client.set_bucket_versioning(
            name, minio_vc.VersioningConfig(status=status)
        )
        LOG.info("Bucket %s versioning set to %s", name, status)

    def set_bucket_policy(self, bucket_name, policy_json):
        self._client.set_bucket_policy(bucket_name, json.dumps(policy_json))
        LOG.info("Bucket %s policy updated", bucket_name)

    def delete_bucket_policy(self, bucket_name):
        self._client.delete_bucket_policy(bucket_name)

    def set_object_lock_config(self, bucket_name, mode, days):
        if mode and days:
            retention_mode = (
                minio_retention.GOVERNANCE
                if mode == "GOVERNANCE"
                else minio_retention.COMPLIANCE
            )
            config = minio_olc.ObjectLockConfig(retention_mode, days, minio_olc.DAYS)
            self._client.set_object_lock_config(bucket_name, config)
            LOG.info(
                "Bucket %s object lock: mode=%s, days=%d",
                bucket_name,
                mode,
                days,
            )

    def set_bucket_public_readonly(self, bucket_name, public):
        if public:
            policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": ["*"]},
                        "Action": ["s3:GetObject"],
                        "Resource": [f"arn:aws:s3:::{bucket_name}/*"],
                    },
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": ["*"]},
                        "Action": ["s3:ListBucket"],
                        "Resource": [f"arn:aws:s3:::{bucket_name}"],
                    },
                ],
            }
            self.set_bucket_policy(bucket_name, policy)
            LOG.info("Bucket %s set to public read", bucket_name)
        else:
            self.delete_bucket_policy(bucket_name)
            LOG.info("Bucket %s public access removed", bucket_name)

    @staticmethod
    def _is_public_read_policy(bucket_name, policy_doc):
        statements = policy_doc.get("Statement", [])
        if not isinstance(statements, list):
            return False

        resource = f"arn:aws:s3:::{bucket_name}/*"
        for statement in statements:
            if statement.get("Effect") != "Allow":
                continue
            principal = statement.get("Principal", {})
            principal_aws = (
                principal.get("AWS") if isinstance(principal, dict) else None
            )
            if principal_aws not in ("*", ["*"]):
                continue

            action = statement.get("Action", [])
            if isinstance(action, str):
                action = [action]
            if "s3:GetObject" not in action:
                continue

            statement_resource = statement.get("Resource", [])
            if isinstance(statement_resource, str):
                statement_resource = [statement_resource]
            if resource in statement_resource:
                return True

        return False

    def get_bucket_state(self, bucket_name):
        state = {
            "versioning_enabled": False,
            "object_lock_enabled": False,
            "public": False,
            "default_retention_mode": None,
            "default_retention_days": None,
        }

        versioning = self._client.get_bucket_versioning(bucket_name)
        state["versioning_enabled"] = versioning.status == minio_vc.ENABLED

        try:
            lock_config = self._client.get_object_lock_config(bucket_name)
            mode = getattr(lock_config, "mode", None)
            duration = getattr(lock_config, "duration", None)
            duration_unit = getattr(lock_config, "duration_unit", None)
            days = None
            if isinstance(duration, int):
                if duration_unit in (minio_olc.DAYS, "Days"):
                    days = duration
            elif duration is not None:
                days = getattr(duration, "days", None)
            if mode:
                state["object_lock_enabled"] = True
                state["default_retention_mode"] = mode
                state["default_retention_days"] = days
        except minio_error.S3Error as exc:
            if exc.code in {
                "ObjectLockConfigurationNotFoundError",
                "NoSuchObjectLockConfiguration",
            }:
                pass  # No object lock configured — expected
            else:
                raise

        try:
            policy_json = self._client.get_bucket_policy(bucket_name)
            if policy_json:
                policy_doc = json.loads(policy_json)
                state["public"] = self._is_public_read_policy(bucket_name, policy_doc)
        except minio_error.S3Error as exc:
            if exc.code != "NoSuchBucketPolicy":
                raise

        return state

    # -- User operations (admin API) --

    def list_users(self):
        try:
            resp = self._admin_request("GET", "/list-users")
            # RustFS returns {"username": {"status": "...", "policyName": "..."}}
            data = resp.json()
            result = {}
            for access_key, info in data.items():
                result[access_key] = {
                    "accessKey": access_key,
                    "status": info.get("status", "enabled"),
                    "policyName": info.get("policyName"),
                    "memberOf": info.get("memberOf", []),
                }
            return result
        except Exception:
            LOG.error("Failed to list users via admin API", exc_info=True)
            raise

    def add_user(self, access_key, secret_key):
        try:
            encoded_key = urllib.parse.quote(access_key, safe="")
            self._admin_request(
                "PUT",
                f"/add-user?accessKey={encoded_key}",
                json_data={"secretKey": secret_key, "status": "enabled"},
            )
            LOG.info("User %s created", access_key)
        except Exception:
            LOG.error("Failed to create user %s", access_key, exc_info=True)
            raise

    def remove_user(self, access_key):
        try:
            encoded_key = urllib.parse.quote(access_key, safe="")
            self._admin_request("DELETE", f"/remove-user?accessKey={encoded_key}")
            LOG.info("User %s removed", access_key)
        except Exception:
            LOG.warning("Failed to remove user %s", access_key, exc_info=True)

    def set_user_policies(self, access_key, policy_names):
        """Attach multiple policies to a user using policy attach endpoint."""
        try:
            # RustFS supports comma-separated policy names, each must be URL-encoded
            encoded_policies = [urllib.parse.quote(p, safe="") for p in policy_names]
            policy_list = ",".join(encoded_policies)
            encoded_user = urllib.parse.quote(access_key, safe="")
            self._admin_request(
                "PUT",
                f"/set-user-or-group-policy?policyName={policy_list}&userOrGroup={encoded_user}&isGroup=false",
            )
            LOG.info("User %s policies set to %s", access_key, ", ".join(policy_names))
        except Exception:
            LOG.error("Failed to set policies for user %s", access_key, exc_info=True)
            raise

    # -- Policy operations (admin API) --

    def list_policies(self):
        try:
            resp = self._admin_request("GET", "/list-canned-policies")
            # RustFS returns {"policyName": {...policy document...}}
            data = resp.json()
            return {
                name: {"name": name, "policy": policy_doc}
                for name, policy_doc in data.items()
                if name not in SYSTEM_POLICIES
            }
        except Exception:
            LOG.error("Failed to list policies via admin API", exc_info=True)
            raise

    def add_policy(self, name, content):
        try:
            # content is a dict, convert to JSON string for RustFS
            if isinstance(content, dict):
                content = json.dumps(content)
            encoded_name = urllib.parse.quote(name, safe="")
            self._admin_request(
                "PUT",
                f"/add-canned-policy?name={encoded_name}",
                body=content.encode("utf-8") if isinstance(content, str) else None,
            )
            LOG.info("Policy %s created/updated", name)
        except Exception:
            LOG.error("Failed to create policy %s", name, exc_info=True)
            raise

    def remove_policy(self, name):
        try:
            encoded_name = urllib.parse.quote(name, safe="")
            self._admin_request("DELETE", f"/remove-canned-policy?name={encoded_name}")
            LOG.info("Policy %s removed", name)
        except Exception:
            LOG.warning("Failed to remove policy %s", name, exc_info=True)


class S3Instance(meta.MetaDataPlaneModel):
    """Data plane model for a single S3/rustfs node.

    Reconciles target state (from control plane) with actual state
    on the local rustfs instance.
    """

    name = properties.property(
        ra_types.String(min_length=1, max_length=512),
        required=True,
    )
    buckets = properties.property(ra_types.Dict(), default=dict)
    users = properties.property(ra_types.Dict(), default=dict)
    policies = properties.property(ra_types.Dict(), default=dict)
    access_keys = properties.property(ra_types.Dict(), default=dict)
    status = properties.property(
        ra_types.Enum([s.value for s in pc.InstanceStatus]),
        default=pc.InstanceStatus.ACTIVE.value,
    )

    _meta_fields = {"uuid", "name"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mc = AdminClient()

    def get_meta_model_fields(self) -> set[str] | None:
        return self._meta_fields

    # -- Reconciliation: policies --

    def _reconcile_policies(self, actual_policies):
        target_policy_uuids = set(self.policies.keys())
        actual_set = set(actual_policies.keys())

        # Create/update target policies only if missing or changed
        for policy_uuid in target_policy_uuids:
            p = self.policies[policy_uuid]
            target_content = p.get("content", {})
            actual_content = actual_policies.get(policy_uuid, {}).get("policy")
            if policy_uuid not in actual_set or not _policy_content_equal(
                target_content, actual_content
            ):
                self.mc.add_policy(policy_uuid, target_content)

        # Remove policies that are no longer in target state.
        # Skip known system policies that cannot be deleted.
        for aname in actual_set - target_policy_uuids:
            if aname not in SYSTEM_POLICIES:
                self.mc.remove_policy(aname)

    def _fill_actual_policies(self):
        actual = self.mc.list_policies()
        self.policies = {
            name: {"name": name, "content": p.get("policy", {}), "builtin": False}
            for name, p in actual.items()
        }

    # -- Reconciliation: users --

    def _reconcile_users(self, actual_users):
        # Only access keys are created as RustFS users — parent users
        # from the CP model are just logical groupings for policies.
        target_users = set(self.access_keys.keys())
        actual_set = set(actual_users.keys())

        def _target_policy_str(user_model):
            policies = (
                list(user_model["policies"].keys())
                if user_model.get("policies")
                else []
            )
            return policies, ",".join(sorted(policies)) if policies else ""

        # Create access-key users (regular RustFS users with parent's
        # policies) — RustFS service accounts always inherit from the
        # admin signing key, so we use regular users instead.
        for ak in target_users - actual_set:
            k = self.access_keys[ak]
            self.mc.add_user(ak, k["secret_key"])
            LOG.info("Access key user %s created", ak)

            parent_user = k.get("user_name")
            if parent_user and parent_user in self.users:
                policy_names, _ = _target_policy_str(self.users[parent_user])
                if policy_names:
                    self.mc.set_user_policies(ak, policy_names)

        # Update policies for existing access-key users
        for ak in target_users & actual_set:
            k = self.access_keys[ak]
            parent_user = k.get("user_name")
            if parent_user and parent_user in self.users:
                target_policies, target_policy_str = _target_policy_str(
                    self.users[parent_user]
                )
                current_policy_names = [
                    name
                    for name in (actual_users[ak].get("policyName") or "").split(",")
                    if name
                ]
                current_policy_str = ",".join(sorted(current_policy_names))
                if current_policy_str != target_policy_str:
                    self.mc.set_user_policies(ak, target_policies)

        # Remove users no longer in target
        for aname in actual_set - target_users:
            self.mc.remove_user(aname)

    def _fill_actual_users(self):
        actual = self.mc.list_users()
        self.users = {
            name: {
                "uuid": "",
                "policies": {
                    u.get("policyName", "readwrite"): {
                        "name": u.get("policyName", "readwrite"),
                        "content": {},
                    }
                }
                if u.get("policyName")
                else {},
            }
            for name, u in actual.items()
        }

    # -- Reconciliation: buckets --

    def _reconcile_buckets(self, actual_buckets):
        target_buckets = set(self.buckets.keys())
        actual_set = set(actual_buckets.keys())

        # Create buckets that exist in target but not in actual
        for bname in target_buckets - actual_set:
            b = self.buckets[bname]
            self.mc.make_bucket(bname, object_lock=b.get("object_lock_enabled"))

        # Apply settings only if different from actual state
        for bname in target_buckets:
            b = self.buckets[bname]
            actual_state = self.mc.get_bucket_state(bname)

            target_versioning = b.get("versioning_enabled", False)
            if actual_state.get("versioning_enabled") != target_versioning:
                self.mc.set_bucket_versioning(bname, target_versioning)

            target_object_lock = b.get("object_lock_enabled", False)
            if target_object_lock:
                actual_mode = actual_state.get("default_retention_mode")
                actual_days = actual_state.get("default_retention_days")
                target_mode = b.get("default_retention_mode")
                target_days = b.get("default_retention_days")
                if actual_mode != target_mode or actual_days != target_days:
                    self.mc.set_object_lock_config(bname, target_mode, target_days)

            target_public = b.get("public", False)
            if actual_state.get("public") != target_public:
                self.mc.set_bucket_public_readonly(bname, target_public)

        # Remove buckets no longer in target
        for aname in actual_set - target_buckets:
            self.mc.remove_bucket(aname)

    def _fill_actual_buckets(self):
        actual = self.mc.list_buckets()
        self.buckets = {}
        for name in actual:
            state = self.mc.get_bucket_state(name)
            self.buckets[name] = {
                "versioning_enabled": state.get("versioning_enabled", False),
                "quota_bytes": 0,
                "object_lock_enabled": state.get("object_lock_enabled", False),
                "public": state.get("public", False),
                "default_retention_mode": state.get("default_retention_mode"),
                "default_retention_days": state.get("default_retention_days"),
            }

    # -- MetaDataPlaneModel interface --

    def dump_to_dp(self) -> None:
        # Fetch actual state once to avoid repeated HTTP calls
        actual_policies = self.mc.list_policies()
        actual_users = self.mc.list_users()
        actual_buckets = self.mc.list_buckets()

        self._reconcile_policies(actual_policies)
        self._reconcile_users(actual_users)
        self._reconcile_buckets(actual_buckets)

    def restore_from_dp(self) -> None:
        self._fill_actual_policies()
        self._fill_actual_users()
        self._fill_actual_buckets()

    def delete_from_dp(self) -> None:
        # Instance exists along with the VM — nothing to delete
        pass

    def update_on_dp(self) -> None:
        self.dump_to_dp()


class S3CapabilityDriver(meta.MetaFileStorageAgentDriver):
    """S3 capability driver for the universal agent."""

    S3_META_PATH = os.path.join(c.WORK_DIR, "s3_meta.json")

    __model_map__ = {
        "s3_instance_node": S3Instance,
    }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, meta_file=self.S3_META_PATH, **kwargs)
