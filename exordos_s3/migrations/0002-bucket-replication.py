#    Copyright 2026 Genesis Corporation.
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

from restalchemy.storage.sql import migrations


class MigrationStep(migrations.AbstarctMigrationStep):
    def __init__(self):
        self._depends = ["0001-distributed-kind.py"]

    @property
    def migration_id(self):
        return "c6a7356c-9de2-4cbf-8dd9-7f401480f1c0"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
CREATE TABLE s3_replication_targets (
    uuid UUID PRIMARY KEY, name VARCHAR(255), description TEXT,
    project_id UUID NOT NULL, bucket UUID NOT NULL REFERENCES s3_buckets(uuid),
    endpoint VARCHAR(2048) NOT NULL, destination_bucket VARCHAR(63) NOT NULL,
    region VARCHAR(255) NOT NULL DEFAULT 'us-east-1',
    access_key VARCHAR(256) NOT NULL, secret_key VARCHAR(4096) NOT NULL,
    session_token TEXT NOT NULL DEFAULT '', ca_cert_pem TEXT NOT NULL DEFAULT '',
    bucket_lookup VARCHAR(16) NOT NULL DEFAULT 'auto',
    storage_class VARCHAR(255) NOT NULL DEFAULT 'STANDARD',
    bandwidth_limit BIGINT NOT NULL DEFAULT 0 CHECK (bandwidth_limit >= 0),
    synchronous BOOLEAN NOT NULL DEFAULT FALSE,
    disable_proxy BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL,
    UNIQUE (bucket, endpoint)
);
CREATE INDEX ON s3_replication_targets(project_id);
CREATE TABLE s3_replication_rules (
    uuid UUID PRIMARY KEY, name VARCHAR(255), description TEXT,
    project_id UUID NOT NULL,
    target UUID NOT NULL REFERENCES s3_replication_targets(uuid),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    priority INT NOT NULL DEFAULT 1 CHECK (priority > 0),
    prefix VARCHAR(1024) NOT NULL DEFAULT '', tags JSONB NOT NULL DEFAULT '{}',
    replicate_existing BOOLEAN NOT NULL DEFAULT TRUE,
    replicate_delete_markers BOOLEAN NOT NULL DEFAULT TRUE,
    replicate_deletes BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL
);
CREATE INDEX ON s3_replication_rules(project_id);
CREATE INDEX ON s3_replication_rules(target);
""")
        # Rebuild the node targets with replication configuration and credentials.
        session.execute("UPDATE s3_instances SET updated_at = now();")

    def downgrade(self, session):
        session.execute(
            "LOCK TABLE s3_replication_targets, s3_replication_rules IN ACCESS EXCLUSIVE MODE;"
        )
        active = session.execute(
            "SELECT EXISTS (SELECT 1 FROM s3_replication_targets) AS active;"
        ).fetchone()
        if active["active"]:
            raise RuntimeError(
                "Remove replication targets and wait for agent cleanup before downgrade"
            )
        session.execute("DROP TABLE s3_replication_rules;")
        session.execute("DROP TABLE s3_replication_targets;")


migration_step = MigrationStep()
