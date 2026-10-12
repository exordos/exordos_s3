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

import copy
import logging
from urllib.parse import urlencode
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from gcl_sdk.agents.universal.storage.common import JsonFileStorageSingleton
from minio.error import S3Error
from minio.helpers import md5sum_hash

LOG = logging.getLogger(__name__)
NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/"
RULE_PREFIX = "exordos:"
ET.register_namespace("", NAMESPACE)


def _child(parent, name, value=None):
    node = ET.SubElement(parent, f"{{{NAMESPACE}}}{name}")
    if value is not None:
        node.text = str(value)
    return node


def _text(parent, path):
    return parent.findtext(path, default="", namespaces={"s": NAMESPACE})


def _status(value):
    return "Enabled" if value else "Disabled"


def rule_xml(rule_id, rule, arn):
    node = ET.Element(f"{{{NAMESPACE}}}Rule")
    _child(node, "ID", RULE_PREFIX + rule_id)
    _child(node, "Priority", rule["priority"])
    _child(node, "Status", _status(rule["enabled"]))
    filters = _child(node, "Filter")
    tags = rule["tags"]
    parent = (
        _child(filters, "And")
        if tags and (rule["prefix"] or len(tags) > 1)
        else filters
    )
    if rule["prefix"] or not tags:
        _child(parent, "Prefix", rule["prefix"])
    for key, value in sorted(tags.items()):
        tag = _child(parent, "Tag")
        _child(tag, "Key", key)
        _child(tag, "Value", value)
    for field, name in (
        ("replicate_existing", "ExistingObjectReplication"),
        ("replicate_delete_markers", "DeleteMarkerReplication"),
        ("replicate_deletes", "DeleteReplication"),
    ):
        _child(_child(node, name), "Status", _status(rule[field]))
    _child(
        _child(_child(node, "SourceSelectionCriteria"), "ReplicaModifications"),
        "Status",
        "Disabled",
    )
    _child(_child(node, "Destination"), "Bucket", arn)
    return node


def canonical_xml(node):
    # XML element order and namespace prefixes do not change rule semantics.
    return (
        node.tag,
        (node.text or "") if not len(node) else "",
        tuple(sorted(node.attrib.items())),
        tuple(sorted(canonical_xml(child) for child in node)),
    )


def target_payload(target, credentials, bucket, arn=""):
    url = urlsplit(target["endpoint"])
    return {
        "sourcebucket": bucket,
        "endpoint": url.netloc,
        "targetbucket": target["destination_bucket"],
        "secure": url.scheme == "https",
        "credentials": {
            "accessKey": target["access_key"],
            "secretKey": credentials["secret_key"],
            "sessionToken": credentials.get("session_token", ""),
        },
        "type": "replication",
        "api": "s3v4",
        "arn": arn,
        "region": target["region"],
        "path": target["bucket_lookup"],
        "storageclass": target["storage_class"],
        "bandwidthlimit": target["bandwidth_limit"],
        "replicationSync": target["synchronous"],
        "disableProxy": target["disable_proxy"],
        "caCertPem": target["ca_cert_pem"],
        "skipTlsVerify": False,
    }


def target_matches(actual, target):
    url = urlsplit(target["endpoint"])
    return all(
        (
            actual.get("endpoint") == url.netloc,
            actual.get("targetbucket") == target["destination_bucket"],
            actual.get("secure", False) == (url.scheme == "https"),
            actual.get("region", "") == target["region"],
            actual.get("credentials", {}).get("accessKey") == target["access_key"],
            actual.get("path", "") == target["bucket_lookup"],
            actual.get("storage_class", actual.get("storageclass", ""))
            == target["storage_class"],
            actual.get("bandwidth_limit", actual.get("bandwidthlimit", 0))
            == target["bandwidth_limit"],
            actual.get("replicationSync", False) == target["synchronous"],
            actual.get("disableProxy", False) == target["disable_proxy"],
            actual.get("caCertPem", "") == target["ca_cert_pem"],
            not actual.get("skipTlsVerify", False),
        )
    )


class ReplicationManager:
    """Reconcile Exordos rules without owning foreign rules or remote data.

    The journal contains ARNs and credential revisions, never credential values.
    A binding is persisted before writing rules so a retry can reuse the target.
    """

    def __init__(self, client, journal_path):
        self.client = client
        self.journal = JsonFileStorageSingleton.get_instance(journal_path)
        self.journal.setdefault("bindings", {})

    @property
    def bindings(self):
        return self.journal["bindings"]

    def _targets(self, bucket):
        response = self.client._admin_request(
            "GET", "/list-remote-targets?" + urlencode({"bucket": bucket})
        )
        return {target["arn"]: target for target in response.json()}

    def _config(self, bucket):
        try:
            response = self.client.client._execute(
                "GET", bucket, query_params={"replication": ""}
            )
            config = ET.fromstring(response.data)
            # RustFS GET omits the S3 namespace that PUT accepts.
            for node in config.iter():
                if not node.tag.startswith("{"):
                    node.tag = f"{{{NAMESPACE}}}{node.tag}"
            return config
        except S3Error as error:
            if error.code != "ReplicationConfigurationNotFoundError":
                raise
            return ET.Element(f"{{{NAMESPACE}}}ReplicationConfiguration")

    def _desired(self, config, targets):
        desired = copy.deepcopy(config)
        for node in list(desired):
            if node.tag == f"{{{NAMESPACE}}}Rule" and _text(node, "s:ID").startswith(
                RULE_PREFIX
            ):
                desired.remove(node)
        foreign = desired.findall("s:Rule", {"s": NAMESPACE})
        if targets and foreign and _text(desired, "s:Role"):
            raise ValueError("foreign replication Role conflicts with managed targets")
        if not foreign:
            role = desired.find("s:Role", {"s": NAMESPACE})
            if any(target["rules"] for target in targets.values()):
                if role is None:
                    desired.insert(0, ET.Element(f"{{{NAMESPACE}}}Role"))
                else:
                    role.text = ""
            elif role is not None:
                desired.remove(role)
        priorities = {int(_text(rule, "s:Priority") or 0) for rule in foreign}
        for target_id, target in targets.items():
            arn = self.bindings[target_id]["arn"]
            for rule_id, rule in target["rules"].items():
                if rule["priority"] in priorities:
                    raise ValueError("replication priority conflicts with another rule")
                priorities.add(rule["priority"])
                desired.append(rule_xml(rule_id, rule, arn))
        return desired

    def _save_config(self, bucket, desired):
        if desired.findall("s:Rule", {"s": NAMESPACE}):
            data = ET.tostring(desired, encoding="utf-8")
            self.client.client._execute(
                "PUT",
                bucket,
                query_params={"replication": ""},
                body=data,
                headers={
                    "Content-Type": "application/xml",
                    "Content-MD5": md5sum_hash(data),
                },
            )
        else:
            self.client.client._execute(
                "DELETE", bucket, query_params={"replication": ""}
            )

    def _ensure_target(self, bucket, target_id, target, credentials, actual, config):
        binding = self.bindings.get(target_id)
        if binding is None or not binding["arn"]:
            # A durable pending intent can recover an ambiguous create response.
            owned = {RULE_PREFIX + rule_id for rule_id in target["rules"]}
            arns = {
                _text(rule, "s:Destination/s:Bucket")
                for rule in config.findall("s:Rule", {"s": NAMESPACE})
                if _text(rule, "s:ID") in owned
            }
            matches = [
                remote
                for remote in actual.values()
                if remote.get("endpoint") == urlsplit(target["endpoint"]).netloc
            ]
            if matches:
                remote = matches[0]
                if (
                    remote["arn"] not in arns
                    and not (
                        binding
                        and binding.get("pending")
                        and target_matches(remote, binding["pending"])
                    )
                ) or remote.get("targetbucket") != target["destination_bucket"]:
                    raise ValueError(
                        "replication endpoint already has an unmanaged target"
                    )
                binding = {"bucket": bucket, "arn": remote["arn"], "revision": ""}
                self.bindings[target_id] = binding
            else:
                self.bindings[target_id] = {
                    "bucket": bucket,
                    "arn": "",
                    "revision": "",
                    "pending": {
                        key: value for key, value in target.items() if key != "rules"
                    },
                }
                self.journal.persist()
                response = self.client._admin_request(
                    "PUT",
                    "/set-remote-target?" + urlencode({"bucket": bucket}),
                    json_data=target_payload(target, credentials, bucket),
                )
                binding = {
                    "bucket": bucket,
                    "arn": response.json(),
                    "revision": target["credential_revision"],
                }
                self.bindings[target_id] = binding
                self.journal.persist()
                return
        remote = actual.get(binding["arn"])
        if remote is None:
            del self.bindings[target_id]
            self.journal.persist()
            return self._ensure_target(
                bucket, target_id, target, credentials, actual, config
            )
        if (
            not target_matches(remote, target)
            or binding["revision"] != target["credential_revision"]
        ):
            query = {
                "bucket": bucket,
                "update": "true",
                "creds": "true",
                "sync": "true",
                "proxy": "true",
                "bandwidth": "true",
                "path": "true",
            }
            self.client._admin_request(
                "PUT",
                "/set-remote-target?" + urlencode(query),
                json_data=target_payload(target, credentials, bucket, binding["arn"]),
            )
            binding["revision"] = target["credential_revision"]
            self.journal.persist()

    def apply(self, desired, credentials, existing_buckets, buckets=None):
        success = True
        if buckets is None:
            buckets = set(desired) | {
                binding["bucket"] for binding in self.bindings.values()
            }
        for bucket in sorted(buckets):
            try:
                targets = desired.get(bucket, {})
                if bucket not in existing_buckets:
                    if targets:
                        raise ValueError("source bucket is not available")
                    for key in list(self.bindings):
                        if self.bindings[key]["bucket"] == bucket:
                            del self.bindings[key]
                    self.journal.persist()
                    continue
                actual = self._targets(bucket)
                config = self._config(bucket)
                for key, target in targets.items():
                    self._ensure_target(
                        bucket, key, target, credentials.get(key, {}), actual, config
                    )
                new_config = self._desired(config, targets)
                if canonical_xml(config) != canonical_xml(new_config):
                    self._save_config(bucket, new_config)
                stale = {
                    key: binding
                    for key, binding in self.bindings.items()
                    if binding["bucket"] == bucket and key not in targets
                }
                if stale:
                    remaining = self._targets(bucket)
                    references = {
                        _text(rule, "s:Destination/s:Bucket")
                        for rule in new_config.findall("s:Rule", {"s": NAMESPACE})
                    }
                    for key, binding in stale.items():
                        arn = binding["arn"] or next(
                            (
                                arn
                                for arn, remote in remaining.items()
                                if binding.get("pending")
                                and target_matches(remote, binding["pending"])
                            ),
                            "",
                        )
                        if arn in references:
                            raise ValueError(
                                "removed target is referenced by a foreign rule"
                            )
                        if arn in remaining:
                            self.client._admin_request(
                                "DELETE",
                                "/remove-remote-target?"
                                + urlencode({"bucket": bucket, "arn": arn}),
                            )
                        del self.bindings[key]
                    self.journal.persist()
            except Exception:
                # Remote error bodies can contain credentials. Report identifiers only.
                LOG.warning(
                    "Cannot apply replication configuration for bucket %s", bucket
                )
                success = False
        return success

    def matches(self, desired):
        try:
            if any(
                key not in desired.get(binding["bucket"], {})
                for key, binding in self.bindings.items()
            ):
                return False
            for bucket, targets in desired.items():
                actual = self._targets(bucket)
                for key, target in targets.items():
                    binding = self.bindings.get(key)
                    if (
                        binding is None
                        or binding["arn"] not in actual
                        or binding["revision"] != target["credential_revision"]
                        or not target_matches(actual[binding["arn"]], target)
                    ):
                        return False
                config = self._config(bucket)
                if canonical_xml(config) != canonical_xml(
                    self._desired(config, targets)
                ):
                    return False
            return True
        except Exception:
            return False
