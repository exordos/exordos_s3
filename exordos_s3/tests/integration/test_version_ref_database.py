# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0.

"""Opt-in migration checks in a disposable PostgreSQL schema."""

import importlib.util
import os
from pathlib import Path
import uuid
from unittest import mock

import gcl_sdk
import psycopg
from psycopg import sql
import pytest

from exordos_s3 import version_ref
from exordos_s3.controlplane.dm import models
from exordos_s3.controlplane.infra.services.builder import CoreInfraBuilder
from exordos_s3.tests.integration import test_replication_database
from exordos_s3.tests.unit.test_models import _instance

database = test_replication_database.database


@pytest.mark.parametrize("image", ["urn:images:new", "https://host:8443/image.raw"])
def test_version_ref_migration_backfill_and_downgrade(image):
    url = os.environ.get("EXORDOS_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("set EXORDOS_TEST_POSTGRES_URL for isolated schema tests")
    # The entire schema, including DDL and downgrade, is rolled back on exit.
    with psycopg.connect(url) as conn:
        try:
            schema = "version_ref_test_" + uuid.uuid4().hex
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            conn.execute(
                sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema))
            )
            migration = None
            for path in sorted(
                Path(__file__).parents[2].joinpath("migrations").glob("[0-9]*.py")
            ):
                spec = importlib.util.spec_from_file_location(path.stem, path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if path.name == "0003-version-ref.py":
                    migration = module.migration_step
                    break
                module.migration_step.upgrade(conn)
            assert migration is not None
            version_id = uuid.uuid4()
            conn.execute(
                "INSERT INTO s3_versions (uuid, name, image, created_at, updated_at) "
                "VALUES (%s, 'existing-version', %s, now(), now())",
                (version_id, image),
            )
            conn.execute(
                "INSERT INTO s3_instances "
                "(uuid, name, project_id, cpu, ram, disk_size, nodes_number, "
                "root_secret, version, created_at, updated_at) "
                "VALUES (%s, 'existing', %s, 1, 1024, 10, 1, 'test', %s, now(), now())",
                (uuid.uuid4(), uuid.uuid4(), version_id),
            )
            migration.upgrade(conn)
            expected = version_ref.build_version_ref(str(version_id), image)
            assert conn.execute("SELECT version_ref FROM s3_versions").fetchone() == (
                expected,
            )
            assert conn.execute("SELECT version_ref FROM s3_instances").fetchone() == (
                expected,
            )
            migration.downgrade(conn)
            assert conn.execute(
                "SELECT to_regprocedure('s3_versions_sync_version_ref()')"
            ).fetchone() == (None,)
            conn.execute("UPDATE s3_versions SET image = 'urn:images:after-downgrade'")
            # Re-applying after downgrade must backfill the current image.
            migration.upgrade(conn)
            assert conn.execute(
                "SELECT version_ref FROM s3_versions WHERE uuid = %s", (version_id,)
            ).fetchone() == (
                version_ref.build_version_ref(
                    str(version_id), "urn:images:after-downgrade"
                ),
            )
        finally:
            conn.rollback()


@pytest.mark.parametrize("image", ["urn:images:new", "https://host:8443/image.raw"])
def test_startup_repairs_downgraded_writes_without_changing_instances(database, image):
    with database.session_manager() as session:
        # Real audit writes are part of the repair transaction.
        for name in ("0007-init-audit-events-4f3a2b.py", "0008-audit-actor-a83d71.py"):
            path = Path(gcl_sdk.__file__).parent / "migrations" / name
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.migration_step.upgrade(session)
        stale = models.S3Version(name="stale", image="urn:images:old")
        missing = models.S3Version(name="missing", image=image)
        correct = models.S3Version(name="correct", image=image)
        for version in (stale, missing, correct):
            version.insert(session=session)
        instance = _instance(version=stale)
        instance.insert(session=session)
        # Reproduce an old package writing after 0003 has already been applied.
        session.execute(
            "UPDATE s3_versions SET image = %s WHERE uuid = %s", (image, stale.uuid)
        )
        session.execute(
            "UPDATE s3_versions SET version_ref = '' WHERE uuid = %s", (missing.uuid,)
        )
        before = {
            v.uuid: v.updated_at
            for v in models.S3Version.objects.get_all(session=session)
        }
        instance_before = session.execute("SELECT * FROM s3_instances").fetchall()
        audits_before = session.execute(
            "SELECT count(*) AS n FROM gcl_sdk_audit_events"
        ).fetchone()["n"]

    service = object.__new__(CoreInfraBuilder)
    # Only registration needs Core; run the real startup repair and ORM writes.
    with mock.patch.object(CoreInfraBuilder, "register_ua_service"):
        service._setup()
        with database.session_manager() as session:
            versions = models.S3Version.objects.get_all(session=session)
            for version in versions:
                assert version.image == image
                assert version.version_ref == version_ref.build_version_ref(
                    str(version.uuid), image
                )
                if version.uuid == correct.uuid:
                    assert version.updated_at == before[version.uuid]
                else:
                    assert version.updated_at > before[version.uuid]
            after = {v.uuid: v.updated_at for v in versions}
            assert (
                session.execute("SELECT * FROM s3_instances").fetchall()
                == instance_before
            )
            assert (
                session.execute(
                    "SELECT count(*) AS n FROM gcl_sdk_audit_events"
                ).fetchone()["n"]
                == audits_before + 2
            )

        service._setup()
        with database.session_manager() as session:
            assert {
                v.uuid: v.updated_at
                for v in models.S3Version.objects.get_all(session=session)
            } == after
            assert (
                session.execute(
                    "SELECT count(*) AS n FROM gcl_sdk_audit_events"
                ).fetchone()["n"]
                == audits_before + 2
            )
