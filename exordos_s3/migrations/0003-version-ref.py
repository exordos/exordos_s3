# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0.

from restalchemy.storage.sql import migrations


class MigrationStep(migrations.AbstarctMigrationStep):
    def __init__(self):
        self._depends = ["0002-bucket-replication.py"]

    @property
    def migration_id(self):
        return "097f1c1f-5ea2-44ba-a3e2-ac4fdc0995db"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            "ALTER TABLE s3_versions ADD COLUMN version_ref VARCHAR(4096) NOT NULL DEFAULT ''; "
        )
        session.execute(
            "ALTER TABLE s3_instances ADD COLUMN version_ref VARCHAR(4096) NOT NULL DEFAULT ''; "
        )
        session.execute("""
UPDATE s3_versions SET version_ref = 'urn:exordos:s3:' || uuid::text || ':' ||
    CASE WHEN image LIKE 'urn:images:%' THEN substring(image FROM 12) ELSE image END;
UPDATE s3_instances AS i SET version_ref = v.version_ref, updated_at = now()
    FROM s3_versions AS v WHERE i.version = v.uuid;
""")

    def downgrade(self, session):
        session.execute("ALTER TABLE s3_instances DROP COLUMN version_ref;")
        session.execute("ALTER TABLE s3_versions DROP COLUMN version_ref;")


migration_step = MigrationStep()
