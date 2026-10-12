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

"""Opt-in ORM and concurrency checks in a disposable PostgreSQL schema."""

import importlib.util
import os
from pathlib import Path
import threading
from unittest import mock
import uuid

import psycopg
from psycopg import sql
import pytest
from restalchemy.storage.sql import engines

from exordos_s3.controlplane.dm import models
from exordos_s3.tests.unit.test_models import _instance


@pytest.fixture
def database():
    url = os.environ.get("EXORDOS_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("set EXORDOS_TEST_POSTGRES_URL for isolated schema tests")
    schema = "replication_test_" + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    previous = engines.engine_factory._engines.get("default")
    engines.engine_factory.configure_factory(
        url,
        config={
            "min_size": 1,
            "max_size": 4,
            "kwargs": {"options": "-c search_path=" + schema},
        },
    )
    engine = engines.engine_factory.get_engine()
    try:
        with engine.session_manager() as session:
            for file in sorted(
                Path(models.__file__)
                .parents[2]
                .joinpath("migrations")
                .glob("[0-9]*.py")
            ):
                spec = importlib.util.spec_from_file_location(
                    "migration_" + file.stem, file
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.migration_step.upgrade(session)
        yield engine
    finally:
        engine._pool.close()
        if previous is None:
            engines.engine_factory._engines.pop("default", None)
        else:
            engines.engine_factory._engines["default"] = previous
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def create_bucket(engine):
    with engine.session_manager() as session:
        engine.get_session_storage().store_session(session)
        try:
            instance = _instance()
            instance.version.insert(session=session)
            instance.insert(session=session)
            bucket = models.S3Bucket(
                name="source-bucket",
                instance=instance,
                project_id=instance.project_id,
                versioning_enabled=True,
            )
            bucket.insert(session=session)
            target = models.S3ReplicationTarget(
                bucket=bucket,
                project_id=instance.project_id,
                endpoint="https://backup.example",
                destination_bucket="backup-bucket",
                access_key="synthetic-key",
                secret_key="synthetic-secret",
            )
            target.insert(session=session)
            return bucket, target
        finally:
            engine.get_session_storage().remove_session()


def test_migrated_replication_crud_and_bucket_cascade(database):
    bucket, target = create_bucket(database)
    with database.session_manager() as session:
        database.get_session_storage().store_session(session)
        try:
            rule = models.S3ReplicationRule(
                target=target, project_id=target.project_id, priority=10
            )
            rule.insert(session=session)
            loaded = models.S3ReplicationTarget.objects.get_one(
                session=session, filters={"uuid": target.uuid}
            )
            assert loaded.secret_key == "synthetic-secret"
            assert len(loaded.get_rules(session=session)) == 1
            rule.enabled = False
            rule.update(session=session)
            target.bandwidth_limit = 8192
            target.secret_key = "rotated-secret"
            target.update(session=session)
            bucket.delete(session=session)
            assert models.S3ReplicationRule.objects.get_all(session=session) == []
            assert models.S3ReplicationTarget.objects.get_all(session=session) == []
        finally:
            database.get_session_storage().remove_session()


def test_database_rule_defaults_preserve_version_history(database):
    bucket, target = create_bucket(database)
    with database.session_manager() as session:
        result = session.execute(
            "INSERT INTO s3_replication_rules "
            "(uuid, project_id, target, created_at, updated_at) "
            "VALUES (%s, %s, %s, now(), now()) "
            "RETURNING replicate_delete_markers, replicate_deletes",
            (str(uuid.uuid4()), str(bucket.project_id), str(target.uuid)),
        ).fetchone()
        assert result == {
            "replicate_delete_markers": True,
            "replicate_deletes": False,
        }


def test_concurrent_rules_cannot_claim_the_same_bucket_priority(database):
    bucket, target = create_bucket(database)
    first = models.S3ReplicationRule(
        target=target, project_id=bucket.project_id, priority=10
    )
    second = models.S3ReplicationRule(
        target=target, project_id=bucket.project_id, priority=10
    )
    validated = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()
    results = []
    original = models.S3ReplicationRule._validate

    def validate(model, session=None):
        if model.uuid == second.uuid:
            second_entered.set()
        original(model, session)
        if model.uuid == first.uuid:
            validated.set()
            assert release.wait(10)

    def create(model):
        try:
            with database.session_manager() as session:
                database.get_session_storage().store_session(session)
                try:
                    model.insert(session=session)
                finally:
                    database.get_session_storage().remove_session()
            results.append("created")
        except models.S3ValidationError:
            results.append("rejected")

    with mock.patch.object(models.S3ReplicationRule, "_validate", validate):
        thread1 = threading.Thread(target=create, args=(first,))
        thread2 = threading.Thread(target=create, args=(second,))
        thread1.start()
        assert validated.wait(10)
        thread2.start()
        try:
            assert not second_entered.wait(0.3), (
                "validation raced before the bucket lock"
            )
        finally:
            release.set()
            thread1.join(timeout=10)
            thread2.join(timeout=10)
    assert sorted(results) == ["created", "rejected"]
    with database.session_manager() as session:
        rows = session.execute("SELECT priority FROM s3_replication_rules").fetchall()
        assert len(rows) == 1


def test_target_creation_uses_locked_bucket_versioning(database):
    bucket, target = create_bucket(database)
    with database.session_manager() as session:
        database.get_session_storage().store_session(session)
        try:
            target.delete(session=session)
            current = models.S3Bucket.objects.get_one(
                session=session, filters={"uuid": bucket.uuid}
            )
            current.versioning_enabled = False
            current.update(session=session)
            assert bucket.versioning_enabled is True
            stale_target = models.S3ReplicationTarget(
                bucket=bucket,
                project_id=bucket.project_id,
                endpoint="https://backup.example",
                destination_bucket="backup-bucket",
                access_key="synthetic-key",
                secret_key="synthetic-secret",
            )
            with pytest.raises(models.S3ValidationError, match="versioning"):
                stale_target.insert(session=session)
        finally:
            database.get_session_storage().remove_session()


def test_downgrade_requires_removal_of_target_rows(database):
    bucket, target = create_bucket(database)
    file = (
        Path(models.__file__).parents[2] / "migrations" / "0002-bucket-replication.py"
    )
    spec = importlib.util.spec_from_file_location("replication_migration", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with database.session_manager() as session:
        with pytest.raises(RuntimeError, match="Remove replication targets"):
            module.migration_step.downgrade(session)
        database.get_session_storage().store_session(session)
        try:
            target.delete(session=session)
            module.migration_step.downgrade(session)
            module.migration_step.upgrade(session)
            assert models.S3ReplicationTarget.objects.get_all(session=session) == []
        finally:
            database.get_session_storage().remove_session()
