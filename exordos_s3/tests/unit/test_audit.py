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
        iam_context=SimpleNamespace(token_info=SimpleNamespace(user_uuid=actor))
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
