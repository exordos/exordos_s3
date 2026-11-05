#    Copyright 2026 Genesis Corporation.
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

from types import SimpleNamespace
from unittest import mock
import uuid

from exordos_metapaas.common import audit
from gcl_sdk.audit.dm import models as sdk_audit_models
import pytest
from restalchemy.common import contexts
from restalchemy.storage.sql import orm

from exordos_s3.controlplane.dm import models


def instance() -> models.S3Instance:
    return models.S3Instance(
        name="test-instance",
        cpu=1,
        ram=512,
        disk_size=8,
        nodes_number=1,
        version=models.S3Version(name="v", image="image"),
        project_id=uuid.uuid4(),
        root_secret="root-canary",
    )


@pytest.mark.parametrize(
    "model",
    [
        models.S3Version,
        models.S3Instance,
        models.S3User,
        models.S3Bucket,
        models.S3Policy,
        models.S3AccessKey,
        models.S3UserPolicyAttachment,
        models.S3ReplicationTarget,
        models.S3ReplicationRule,
    ],
)
def test_business_models_record_s3_events(model: type) -> None:
    assert issubclass(model, audit.AuditLogSQLStorableMixin)
    assert model.get_audit_service_name() == "s3"
    assert model.get_audit_resource_type() == model.__tablename__


@pytest.mark.parametrize("action", ["insert", "update", "delete"])
def test_audit_event_shares_resource_transaction(action: str) -> None:
    version = models.S3Version(name="v", image="image")
    session = mock.Mock()
    engine = mock.MagicMock()
    engine.session_manager.return_value.__enter__.return_value = session
    with (
        mock.patch.object(version, "_get_engine", return_value=engine),
        mock.patch.object(version, "is_dirty", return_value=True),
        mock.patch.object(version, "_write_audit_event") as write,
        mock.patch.object(orm.SQLStorableMixin, action) as mutation,
    ):
        getattr(version, action)()
    assert mutation.call_args.kwargs["session"] is session
    assert write.call_args.kwargs["session"] is session
    assert (
        write.call_args.kwargs["action"]
        == {
            "insert": "create",
            "update": "update",
            "delete": "delete",
        }[action]
    )
    assert (write.call_args.kwargs["snapshot"] is None) == (action == "delete")


def test_credentials_are_absent_from_snapshots() -> None:
    parent = instance()
    user = models.S3User(name="user", instance=parent, project_id=parent.project_id)
    key = models.S3AccessKey(
        name="key",
        user=user,
        instance=parent,
        project_id=parent.project_id,
        access_key="access-canary",
        secret_key="secret-canary",
    )
    assert "root_secret" not in parent.dump_to_audit_snapshot()
    snapshot = key.dump_to_audit_snapshot()
    assert {"access_key", "secret_key"}.isdisjoint(snapshot)
    assert snapshot["user"] == str(user.uuid)
    assert snapshot["instance"] == str(parent.uuid)


@pytest.mark.parametrize("actor", [None, uuid.uuid4()])
def test_audit_event_keeps_user_actor_and_resource_project(actor) -> None:
    parent = instance()
    context = SimpleNamespace(
        iam_context=SimpleNamespace(
            token_info=SimpleNamespace(user_uuid=actor),
            introspection_info=mock.Mock(return_value={}),
        )
    )
    session = mock.Mock()
    with (
        mock.patch.object(contexts, "get_context", return_value=context),
        mock.patch.object(
            sdk_audit_models.AuditDeliveryEvent, "insert", autospec=True
        ) as insert,
    ):
        parent._write_audit_event("create", parent.dump_to_audit_snapshot(), session)
    event = insert.call_args.args[0]
    assert event.actor_user_uuid == actor
    assert event.project_id == parent.project_id
    assert event.resource_uuid == parent.uuid
    assert insert.call_args.args[1] is session


@pytest.fixture
def replication_target():
    parent = instance()
    bucket = models.S3Bucket(
        name="source-bucket",
        instance=parent,
        project_id=parent.project_id,
        versioning_enabled=True,
    )
    return models.S3ReplicationTarget(
        name="backup",
        bucket=bucket,
        project_id=parent.project_id,
        endpoint="https://backup.example",
        destination_bucket="backup-bucket",
        access_key="access-canary",
        secret_key="secret-canary",
        session_token="token-canary",
    )


@pytest.mark.parametrize("resource_type", ["target", "rule"])
@pytest.mark.parametrize("action", ["insert", "update", "delete"])
def test_replication_mutations_record_events(replication_target, resource_type, action):
    target = replication_target
    resource = (
        target
        if resource_type == "target"
        else models.S3ReplicationRule(
            name="photos",
            target=target,
            project_id=target.project_id,
            prefix="photos/",
        )
    )
    session = mock.Mock()
    engine = mock.MagicMock()
    engine.session_manager.return_value.__enter__.return_value = session
    with (
        mock.patch.object(resource, "_get_engine", return_value=engine),
        mock.patch.object(resource, "_validate"),
        mock.patch.object(resource, "is_dirty", return_value=True),
        mock.patch.object(
            models, "_lock_bucket", return_value={"versioning_enabled": True}
        ),
        mock.patch.object(target, "get_rules", return_value=[]),
        mock.patch.object(target.bucket, "touch_parent") as touch,
        mock.patch.object(
            sdk_audit_models.AuditDeliveryEvent, "insert", autospec=True
        ) as insert,
        mock.patch.object(orm.SQLStorableMixin, action) as mutation,
    ):
        getattr(resource, action)(session=session)
    assert mutation.call_args.kwargs["session"] is session
    touch.assert_called_once_with(session=session)
    event = insert.call_args.args[0]
    assert insert.call_args.args[1] is session
    assert event.service_name == "s3"
    assert event.resource_type == resource.__tablename__
    assert event.resource_uuid == resource.uuid
    assert event.project_id == target.project_id
    assert (
        event.action
        == {"insert": "create", "update": "update", "delete": "delete"}[action]
    )
    assert event.snapshot == (
        None if action == "delete" else resource.dump_to_audit_snapshot()
    )


def test_replication_snapshots_exclude_credentials(replication_target):
    target = replication_target
    snapshot = target.dump_to_audit_snapshot()
    assert {"access_key", "secret_key", "session_token"}.isdisjoint(snapshot)
    assert not any(
        value in str(snapshot)
        for value in ("access-canary", "secret-canary", "token-canary")
    )
    assert snapshot["bucket"] == str(target.bucket.uuid)
    assert snapshot["endpoint"] == target.endpoint
    assert snapshot["destination_bucket"] == target.destination_bucket
    rule = models.S3ReplicationRule(
        name="photos", target=target, project_id=target.project_id, prefix="photos/"
    )
    rule_snapshot = rule.dump_to_audit_snapshot()
    assert rule_snapshot["target"] == str(target.uuid)
    assert rule_snapshot["prefix"] == "photos/"
    assert rule_snapshot["enabled"] is True
