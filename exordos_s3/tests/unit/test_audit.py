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

from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import uuid

from gcl_sdk.audit import opts
from gcl_sdk.audit.dm import models as sdk_audit_models
from oslo_config import cfg
import pytest
from restalchemy.common import contexts
from restalchemy.storage.sql import orm
import yaml

from exordos_s3 import audit
from exordos_s3.controlplane.dm import models

ROOT = Path(__file__).parents[3]


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


def test_optional_manifest_config_and_token_scope(tmp_path: Path) -> None:
    text = (ROOT / "exordos/manifests/s3_audit.yaml.j2").read_text()
    manifest = yaml.safe_load(text.replace("{{ version }}", "test"))
    resources = manifest["resources"]
    assert "audit" in manifest["requirements"]
    main = yaml.safe_load((ROOT / "exordos/manifests/s3aas.yaml.j2").read_text())
    assert "audit" not in main["requirements"]
    config = resources["$core.config.configs"]["s3_audit_config"]
    assert config["project_id"] == "12345678-c625-4fee-81d5-f691897b8142"
    assert config["path"] == "/etc/exordos_metapaas/exordos_metapaas.d/audit.conf"
    assert config["mode"] == "0600"
    token = resources["$core.iam.tokens"]["s3_audit_token"]
    assert token["auto_renew"] is True
    assert token["scope"] == 'f"project:{$s3_audit.imports.$metapaas_project:uuid}"'
    content = config["body"]["content"].strip().removeprefix('f"').removesuffix('"')
    content = content.replace(
        "{{ audit_endpoint | default('http://audit.local.genesis-core.tech:8080/') }}",
        "http://audit.example/",
    ).replace("{$core.iam.tokens.$s3_audit_token:access_token}", "test-token")
    path = tmp_path / "audit.conf"
    path.write_text(content)
    conf = cfg.ConfigOpts()
    opts.register_audit_delivery_opts(conf)
    conf(["--config-file", str(path)])
    assert conf.audit_delivery.enabled is True
    assert conf.audit_delivery.auth_token == "test-token"
    assert conf.audit_delivery.endpoint == "http://audit.example/"
