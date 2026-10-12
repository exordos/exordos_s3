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

from contextlib import nullcontext
import copy
import json
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs
from urllib.parse import urlsplit
import uuid
from xml.etree import ElementTree as ET

from minio.error import S3Error
from minio.helpers import md5sum_hash
import pytest

from exordos_s3.controlplane.dm import models
from exordos_s3.controlplane.paas.dm import models as node_models
from exordos_s3.controlplane.paas.services.builder import S3InstanceBuilder
from exordos_s3.dataplane import driver
from exordos_s3.dataplane import replication as r
from exordos_s3.tests.unit.test_models import _instance


def target(**overrides):
    data = dict(
        endpoint="https://backup.example:9443",
        destination_bucket="backup-bucket",
        region="us-east-1",
        access_key="backup-key",
        ca_cert_pem="",
        bucket_lookup="path",
        storage_class="STANDARD",
        bandwidth_limit=4096,
        synchronous=False,
        disable_proxy=True,
        credential_revision="revision-1",
        rules={"rule-1": rule()},
    )
    return data | overrides


def rule(**overrides):
    return (
        dict(
            enabled=True,
            priority=10,
            prefix="",
            tags={},
            replicate_existing=True,
            replicate_delete_markers=True,
            replicate_deletes=False,
        )
        | overrides
    )


CREDS = {"target-1": {"secret_key": "private-secret", "session_token": "private-token"}}


class FakeRustFS:
    """Stateful admin/S3 boundary, including RustFS output field names."""

    def __init__(self):
        self.targets = {}
        self.configs = {}
        self.writes = []
        self.fail_put = False
        self.fail_read = False
        self.client = self

    def _admin_request(self, method, path, json_data=None):
        url = urlsplit(path)
        query = parse_qs(url.query)
        bucket = query["bucket"][0]
        targets = self.targets.setdefault(bucket, {})
        if method == "GET":
            if self.fail_read:
                raise RuntimeError("unavailable private-secret")
            return SimpleNamespace(json=lambda: copy.deepcopy(list(targets.values())))
        self.writes.append((method, url.path, bucket))
        if method == "PUT":
            data = copy.deepcopy(json_data)
            arn = data["arn"] or "arn:aws:s3:::replication:" + str(uuid.uuid4())
            data["arn"] = arn
            data["bandwidth_limit"] = data.pop("bandwidthlimit")
            data["storage_class"] = data.pop("storageclass")
            data["credentials"]["secretKey"] = "<redacted>"
            data["credentials"]["session_token"] = "<redacted>"
            data["credentials"].pop("sessionToken")
            targets[arn] = data
            return SimpleNamespace(json=lambda: arn)
        del targets[query["arn"][0]]
        return SimpleNamespace()

    def _execute(self, method, bucket, query_params, body=None, headers=None):
        assert query_params == {"replication": ""}
        if method == "GET":
            if self.fail_read:
                raise RuntimeError("unavailable")
            if bucket not in self.configs:
                raise S3Error(
                    None, "ReplicationConfigurationNotFoundError", "absent", "/", "", ""
                )
            return SimpleNamespace(data=self.configs[bucket])
        self.writes.append((method, "replication", bucket))
        if method == "PUT":
            assert headers["Content-MD5"] == md5sum_hash(body)
            if self.fail_put:
                raise RuntimeError("remote rejected private-secret")
            self.configs[bucket] = body
        else:
            self.configs.pop(bucket, None)


@pytest.fixture
def manager(tmp_path):
    client = FakeRustFS()
    return r.ReplicationManager(client, str(tmp_path / "journal.json"))


def test_reconcile_is_idempotent_and_secrets_are_not_journaled(manager):
    desired = {"source-bucket": {"target-1": target()}}
    assert manager.apply(desired, CREDS, {"source-bucket"})
    assert manager.matches(desired)
    writes = manager.client.writes.copy()
    assert manager.apply(desired, CREDS, {"source-bucket"})
    assert manager.client.writes == writes
    serialized = json.dumps(manager.journal)
    assert "private-secret" not in serialized and "private-token" not in serialized


@pytest.mark.parametrize("change_rule", [False, True])
def test_cached_target_without_credentials_can_be_reconciled(manager, change_rule):
    desired = {"source-bucket": {"target-1": target()}}
    assert manager.apply(desired, CREDS, {"source-bucket"})
    writes = manager.client.writes.copy()
    if change_rule:
        desired["source-bucket"]["target-1"]["rules"]["rule-1"]["prefix"] = "backup/"
    assert manager.apply(desired, {}, {"source-bucket"})
    assert manager.matches(desired)
    assert manager.client.writes == writes + (
        [("PUT", "replication", "source-bucket")] if change_rule else []
    )


def test_missing_credentials_do_not_acknowledge_rotation(manager):
    desired = {"source-bucket": {"target-1": target()}}
    assert manager.apply(desired, CREDS, {"source-bucket"})
    desired["source-bucket"]["target-1"]["credential_revision"] = "rotated"
    writes = manager.client.writes.copy()
    assert not manager.apply(desired, {}, {"source-bucket"})
    assert not manager.matches(desired)
    assert manager.bindings["target-1"]["revision"] == "revision-1"
    assert manager.client.writes == writes


def test_retry_reuses_registration_after_failed_xml_write(manager, caplog):
    desired = {"source-bucket": {"target-1": target()}}
    manager.client.fail_put = True
    assert not manager.apply(desired, CREDS, {"source-bucket"})
    assert "private-secret" not in caplog.text
    assert not manager.matches(desired)
    assert len(manager.bindings) == 1
    manager.client.fail_put = False
    assert manager.apply(desired, CREDS, {"source-bucket"})
    registrations = [w for w in manager.client.writes if w[1] == "/set-remote-target"]
    assert len(registrations) == 1
    assert manager.matches(desired)


def test_rotation_and_target_options_preserve_arn(manager):
    desired = {"source-bucket": {"target-1": target()}}
    assert manager.apply(desired, CREDS, {"source-bucket"})
    arn = manager.bindings["target-1"]["arn"]
    changed = copy.deepcopy(desired)
    changed["source-bucket"]["target-1"].update(
        credential_revision="revision-2",
        bandwidth_limit=8192,
        synchronous=True,
        disable_proxy=False,
        bucket_lookup="dns",
    )
    assert not manager.matches(changed)
    assert manager.apply(
        changed, {"target-1": {"secret_key": "rotated"}}, {"source-bucket"}
    )
    assert manager.matches(changed)
    assert manager.bindings["target-1"]["arn"] == arn


def test_multiple_destinations_and_selective_decommission(manager):
    desired = {
        "source-bucket": {
            "target-1": target(),
            "target-2": target(
                endpoint="https://another.example", rules={"rule-2": rule(priority=20)}
            ),
        }
    }
    credentials = CREDS | {"target-2": CREDS["target-1"]}
    assert manager.apply(desired, credentials, {"source-bucket"})
    assert manager.matches(desired)
    del desired["source-bucket"]["target-1"]
    assert manager.apply(desired, credentials, {"source-bucket"})
    assert len(manager.client.targets["source-bucket"]) == 1
    assert manager.matches(desired)
    assert manager.apply({}, {}, {"source-bucket"})
    assert not manager.bindings and not manager.client.configs
    assert all(w[2] == "source-bucket" for w in manager.client.writes)


def test_foreign_rule_extensions_are_preserved(manager):
    foreign = r.rule_xml("foreign", rule(priority=50), "foreign-arn")
    foreign.find("s:ID", {"s": r.NAMESPACE}).text = "other-system"
    ET.SubElement(foreign, "{other-schema}Custom").text = "opaque"
    config = ET.Element(f"{{{r.NAMESPACE}}}ReplicationConfiguration")
    config.append(foreign)
    manager.client.configs["source-bucket"] = ET.tostring(config)
    desired = {"source-bucket": {"target-1": target()}}
    assert manager.apply(desired, CREDS, {"source-bucket"})
    assert manager.matches(desired)
    assert manager.apply({}, {}, {"source-bucket"})
    remaining = ET.fromstring(manager.client.configs["source-bucket"])
    assert r.canonical_xml(remaining[0]) == r.canonical_xml(foreign)


@pytest.mark.parametrize("conflict", ["priority", "role"])
def test_foreign_conflicts_refuse_to_overwrite_configuration(manager, conflict):
    config = ET.Element(f"{{{r.NAMESPACE}}}ReplicationConfiguration")
    foreign = r.rule_xml(
        "foreign", rule(priority=10 if conflict == "priority" else 50), "foreign-arn"
    )
    foreign.find("s:ID", {"s": r.NAMESPACE}).text = "foreign"
    config.append(foreign)
    if conflict == "role":
        r._child(config, "Role", "foreign-role")
    original = ET.tostring(config)
    manager.client.configs["source-bucket"] = original
    assert not manager.apply(
        {"source-bucket": {"target-1": target()}}, CREDS, {"source-bucket"}
    )
    assert manager.client.configs["source-bucket"] == original


def test_unmanaged_remote_endpoint_is_not_taken_over(manager):
    remote = r.target_payload(
        target(), CREDS["target-1"], "source-bucket", "foreign-arn"
    )
    manager.client.targets["source-bucket"] = {"foreign-arn": remote}
    assert not manager.apply(
        {"source-bucket": {"target-1": target()}}, CREDS, {"source-bucket"}
    )
    assert manager.client.writes == []


def test_failed_reads_do_not_become_empty_state(manager):
    desired = {"source-bucket": {"target-1": target()}}
    assert manager.apply(desired, CREDS, {"source-bucket"})
    manager.client.fail_read = True
    writes = manager.client.writes.copy()
    assert not manager.apply({}, {}, {"source-bucket"})
    assert not manager.matches(desired)
    assert manager.client.writes == writes
    assert manager.bindings


def test_lost_binding_is_recovered_only_through_owned_rule(manager):
    desired = {"source-bucket": {"target-1": target()}}
    assert manager.apply(desired, CREDS, {"source-bucket"})
    arn = manager.bindings["target-1"]["arn"]
    manager.bindings.clear()
    assert manager.apply(desired, CREDS, {"source-bucket"})
    assert manager.matches(desired)
    assert manager.bindings["target-1"]["arn"] == arn


def test_all_delete_flags_and_filters_survive_xml_roundtrip():
    node = r.rule_xml(
        "rule",
        rule(
            prefix="photos/&",
            tags={"kind": "<&>", "empty": ""},
            replicate_delete_markers=True,
            replicate_deletes=True,
        ),
        "arn",
    )
    parsed = ET.fromstring(ET.tostring(node))
    assert r._text(parsed, "s:DeleteReplication/s:Status") == "Enabled"
    assert r._text(parsed, "s:DeleteMarkerReplication/s:Status") == "Enabled"
    assert r._text(parsed, "s:Filter/s:And/s:Prefix") == "photos/&"
    assert (
        r._text(parsed, "s:SourceSelectionCriteria/s:ReplicaModifications/s:Status")
        == "Disabled"
    )
    ET.indent(parsed)
    assert r.canonical_xml(parsed) == r.canonical_xml(node)


def bucket_target(**changes):
    instance = _instance()
    bucket = models.S3Bucket(
        instance=instance,
        name="source-bucket",
        project_id=instance.project_id,
        versioning_enabled=True,
    )
    model = models.S3ReplicationTarget(
        bucket=bucket,
        project_id=changes.pop("project_id", bucket.project_id),
        endpoint="https://backup.example",
        destination_bucket="backup-bucket",
        access_key="key",
        secret_key="secret",
        **changes,
    )
    return model


def validate_target(model, others=()):
    with mock.patch.object(
        models.S3Bucket, "get_replication_targets", return_value=others
    ):
        model._validate()


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://backup.example",
        "https://user:pass@backup.example",
        "https://backup.example/path",
        "https://backup.example?q=x",
        "http://127.0.0.1:9000",
        "http://169.254.169.254",
        "http://localhost",
        "http://[::1]",
        "https://backup.example:0",
    ],
)
def test_invalid_endpoints(endpoint):
    model = bucket_target()
    model.endpoint = endpoint
    with pytest.raises(models.S3ValidationError):
        validate_target(model)


def test_endpoint_normalization_and_duplicate_destination():
    model = bucket_target()
    model.endpoint = "https://BACKUP.example.:443/"
    validate_target(model)
    assert model.endpoint == "https://backup.example"
    other = bucket_target()
    with pytest.raises(models.S3ValidationError, match="one replication target"):
        validate_target(model, [other])


def test_target_enforces_tenancy_and_source_versioning():
    model = bucket_target(project_id=uuid.uuid4())
    with pytest.raises(models.S3ValidationError, match="same project"):
        validate_target(model)
    model = bucket_target()
    model.bucket.versioning_enabled = False
    with pytest.raises(models.S3ValidationError, match="versioning"):
        validate_target(model)


def test_rule_checks_project_tag_delete_and_bucketwide_priority():
    model = bucket_target()
    existing = models.S3ReplicationRule(
        target=model, project_id=model.project_id, priority=10
    )
    candidate = models.S3ReplicationRule(
        target=model, project_id=model.project_id, priority=20
    )
    with (
        mock.patch.object(
            models.S3Bucket, "get_replication_targets", return_value=[model]
        ),
        mock.patch.object(
            models.S3ReplicationTarget, "get_rules", return_value=[existing]
        ),
    ):
        candidate._validate()
        candidate.priority = 10
        with pytest.raises(models.S3ValidationError, match="unique"):
            candidate._validate()
        candidate.priority = 20
        candidate.tags = {"kind": "photo"}
        candidate.replicate_delete_markers = True
        with pytest.raises(models.S3ValidationError, match="delete markers"):
            candidate._validate()
        candidate.replicate_delete_markers = False
        candidate._validate()
        candidate = models.S3ReplicationRule(target=model, project_id=uuid.uuid4())
        with pytest.raises(models.S3ValidationError, match="same project"):
            candidate._validate()


def test_cannot_suspend_versioning_or_delete_referenced_target():
    model = bucket_target()
    model.bucket.versioning_enabled = False
    engine = mock.Mock()
    engine.session_manager.return_value = nullcontext(mock.Mock())
    with (
        mock.patch.object(models.S3Bucket, "_get_engine", return_value=engine),
        mock.patch.object(
            models.S3Bucket, "get_replication_targets", return_value=[model]
        ),
        pytest.raises(models.S3ValidationError, match="versioning"),
    ):
        model.bucket.update()
    with (
        mock.patch.object(
            models.S3ReplicationTarget, "get_rules", return_value=[object()]
        ),
        pytest.raises(models.S3ValidationError, match="remove replication rules"),
    ):
        model.delete()


def test_controlplane_payload_and_agent_metadata_do_not_report_secrets():
    model = bucket_target()
    config = models.S3ReplicationRule(target=model, project_id=model.project_id)
    with (
        mock.patch.object(
            models.S3Instance, "get_buckets", return_value=[model.bucket]
        ),
        mock.patch.object(
            models.S3Bucket, "get_replication_targets", return_value=[model]
        ),
        mock.patch.object(
            models.S3ReplicationTarget, "get_rules", return_value=[config]
        ),
    ):
        desired, secrets = S3InstanceBuilder.__new__(
            S3InstanceBuilder
        )._get_replication(model.bucket.instance)
    assert "secret" not in json.dumps(desired)
    assert secrets[str(model.uuid)]["secret_key"] == "secret"
    cp = node_models.S3InstanceNode(
        uuid=uuid.uuid4(),
        name="node",
        buckets={},
        users={},
        policies={},
        access_keys={},
        replication=desired,
        replication_credentials=secrets,
    )
    resource = cp.to_ua_resource()
    with mock.patch.object(driver, "AdminClient"):
        dp = driver.S3Instance.from_ua_resource(resource)
    delivered_rule = dp.replication[model.bucket.name][str(model.uuid)]["rules"][
        str(config.uuid)
    ]
    xml = r.rule_xml(str(config.uuid), delivered_rule, "arn")
    assert r._text(xml, "s:DeleteMarkerReplication/s:Status") == "Enabled"
    assert r._text(xml, "s:DeleteReplication/s:Status") == "Disabled"
    assert dp.to_ua_resource(resource.kind).hash == resource.hash
    assert dp.replication_credentials == secrets
    assert "replication_credentials" not in dp.to_ua_resource(resource.kind).value
    meta_view = dp.dump_to_simple_view(
        skip=set(dp.properties.properties) - dp.get_meta_fields()
    )
    assert "replication_credentials" not in meta_view
    assert "secret" not in json.dumps(meta_view, default=str)
    rotated = copy.deepcopy(secrets)
    rotated[str(model.uuid)]["secret_key"] = "another-secret"
    cp.replication_credentials = rotated
    assert cp.to_ua_resource().hash == resource.hash
    cp.replication[model.bucket.name][str(model.uuid)]["credential_revision"] = (
        "rotated-revision"
    )
    assert cp.to_ua_resource().hash != resource.hash


@mock.patch.object(driver, "AdminClient")
def test_legacy_agent_metadata_is_readable_without_replication(_client):
    dp = driver.S3Instance.restore_from_simple_view(
        uuid=str(uuid.uuid4()),
        name="node",
        target_fields=["uuid", "name", "buckets", "users", "policies", "access_keys"],
    )
    assert dp.replication == {} and dp.replication_credentials == {}


def test_ambiguous_registration_is_recovered_after_restart(manager, tmp_path):
    desired = {"source-bucket": {"target-1": target(rules={})}}
    original = manager.client._admin_request

    def lost_response(method, path, json_data=None):
        response = original(method, path, json_data)
        if method == "PUT":
            raise RuntimeError("connection lost after commit")
        return response

    with mock.patch.object(manager.client, "_admin_request", side_effect=lost_response):
        assert not manager.apply(desired, CREDS, {"source-bucket"})
    assert len(manager.client.targets["source-bucket"]) == 1
    assert manager.bindings["target-1"]["arn"] == ""
    # Reload from the file rather than retaining the failed request's memory.
    r.JsonFileStorageSingleton._instances.pop(str(tmp_path / "journal.json"))
    resumed = r.ReplicationManager(manager.client, str(tmp_path / "journal.json"))
    assert resumed.apply(desired, CREDS, {"source-bucket"})
    assert resumed.matches(desired)
    assert len(manager.client.targets["source-bucket"]) == 1


def test_pending_target_can_be_removed_without_an_xml_rule(manager):
    remote = r.target_payload(
        target(), CREDS["target-1"], "source-bucket", "pending-arn"
    )
    manager.client.targets["source-bucket"] = {"pending-arn": remote}
    manager.bindings["target-1"] = {
        "bucket": "source-bucket",
        "arn": "",
        "revision": "",
        "pending": target(),
    }
    assert manager.apply({}, {}, {"source-bucket"})
    assert not manager.client.targets["source-bucket"] and not manager.bindings


def test_foreign_rule_referencing_owned_target_prevents_target_removal(manager):
    desired = {"source-bucket": {"target-1": target()}}
    assert manager.apply(desired, CREDS, {"source-bucket"})
    arn = manager.bindings["target-1"]["arn"]
    config = manager._config("source-bucket")
    foreign = r.rule_xml("foreign", rule(priority=50), arn)
    foreign.find("s:ID", {"s": r.NAMESPACE}).text = "foreign-system"
    config.append(foreign)
    manager.client.configs["source-bucket"] = ET.tostring(config)
    assert not manager.apply({}, {}, {"source-bucket"})
    assert arn in manager.client.targets["source-bucket"]
    assert r._text(manager._config("source-bucket")[1], "s:ID") == "foreign-system"


def test_native_get_xml_without_namespace_settles(manager):
    desired = {"source-bucket": {"target-1": target()}}
    assert manager.apply(desired, CREDS, {"source-bucket"})
    manager.client.configs["source-bucket"] = manager.client.configs[
        "source-bucket"
    ].replace(f' xmlns="{r.NAMESPACE}"'.encode(), b"")
    assert manager.matches(desired)


@pytest.mark.parametrize("field", ["access_key", "secret_key", "session_token"])
def test_credential_fields_are_hidden_from_reads_and_queries(field):
    from restalchemy.api import constants

    from exordos_s3.controlplane.api import controllers

    resource = controllers.S3ReplicationTargetController.get_resource()
    for method in (constants.GET, constants.FILTER, constants.CREATE, constants.UPDATE):
        req = mock.Mock()
        req.api_context.resolved_visibilities = {}
        req.api_context.get_active_method.return_value = method
        req.api_context.shown_fields.side_effect = lambda names: frozenset(names)
        visibility = resource.resolve_visibility(req)
        hidden = method in (constants.GET, constants.FILTER)
        assert visibility.is_hidden(field) == hidden
        assert visibility.is_readonly(field) == hidden
        if method == constants.FILTER:
            controller = controllers.S3ReplicationTargetController.__new__(
                controllers.S3ReplicationTargetController
            )
            controller._req = req
            assert not controller._is_queryable_field(field)


@pytest.mark.parametrize(
    "endpoint", ["https://tenant-controlled.example:9443", "http://10.20.30.40:9000"]
)
def test_target_accepts_user_selected_endpoint(endpoint):
    model = bucket_target()
    model.endpoint = endpoint
    validate_target(model)
    assert model.endpoint == endpoint


def test_unconfigured_node_target_keeps_legacy_fields_and_hash():
    cp = node_models.S3InstanceNode(
        name="node", buckets={}, users={}, policies={}, access_keys={}
    )
    resource = cp.to_ua_resource()
    assert set(resource.value) == {
        "uuid",
        "name",
        "buckets",
        "users",
        "policies",
        "access_keys",
    }
    with mock.patch.object(driver, "AdminClient"):
        dp = driver.S3Instance.from_ua_resource(resource)
    assert dp.to_ua_resource(resource.kind).hash == resource.hash


def test_replication_cleanup_precedes_versioning_suspension():
    with mock.patch.object(driver, "AdminClient"):
        instance = driver.S3Instance(uuid=uuid.uuid4(), name="node")
    instance.mc = mock.Mock()
    instance.mc.list_policies.return_value = {}
    instance.mc.list_users.return_value = {}
    instance.mc.list_buckets.return_value = {"source-bucket": {}}
    manager = mock.Mock()
    manager.bindings = {"old-target": {"bucket": "source-bucket", "arn": "old-arn"}}
    calls = []
    manager.apply.side_effect = lambda *args, **kwargs: calls.append("cleanup") or True
    with (
        mock.patch.object(instance, "_replication_manager", return_value=manager),
        mock.patch.object(
            instance,
            "_fill_ready",
            side_effect=lambda: setattr(instance, "ready", True),
        ),
        mock.patch.object(
            instance,
            "_reconcile_buckets",
            side_effect=lambda actual: calls.append("buckets"),
        ),
    ):
        instance.dump_to_dp()
    assert calls[0:2] == ["cleanup", "buckets"]
    assert manager.apply.call_args_list[0].kwargs == {"buckets": {"source-bucket"}}


def test_failed_last_target_cleanup_changes_hash_until_retry_succeeds():
    cp = node_models.S3InstanceNode(
        name="node", buckets={}, users={}, policies={}, access_keys={}
    )
    resource = cp.to_ua_resource()
    with mock.patch.object(driver, "AdminClient"):
        node = driver.S3Instance.from_ua_resource(resource)
    node.mc = mock.Mock()
    node.mc.list_policies.return_value = {}
    node.mc.list_users.return_value = {}
    node.mc.list_buckets.return_value = {}
    manager = mock.Mock()
    manager.bindings = {"old-target": {"bucket": "source-bucket", "arn": "arn"}}
    manager.apply.return_value = False
    with (
        mock.patch.object(node, "_replication_manager", return_value=manager),
        mock.patch.object(
            node, "_fill_ready", side_effect=lambda: setattr(node, "ready", True)
        ),
    ):
        node.dump_to_dp()
    assert node.replication == {"error": True}
    assert node.to_ua_resource(resource.kind).hash != resource.hash
