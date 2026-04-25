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

from restalchemy.storage.sql import migrations


class MigrationStep(migrations.AbstarctMigrationStep):
    def __init__(self):
        self._depends = []

    @property
    def migration_id(self):
        return "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """\
CREATE TABLE s3_versions (
    uuid UUID PRIMARY KEY,
    name VARCHAR(255) UNIQUE NOT NULL,
    description TEXT,
    image TEXT,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
""",
            """\
CREATE TABLE s3_instances (
    uuid UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    project_id UUID NOT NULL,
    status VARCHAR(64) NOT NULL DEFAULT 'NEW',
    cpu INT NOT NULL CHECK (cpu BETWEEN 1 AND 128),
    ram INT NOT NULL CHECK (ram BETWEEN 512 AND 1073741824),
    disk_size INT NOT NULL CHECK (disk_size BETWEEN 8 AND 1073741824),
    nodes_number INT NOT NULL CHECK (nodes_number BETWEEN 1 AND 16),
    kind VARCHAR(32) NOT NULL DEFAULT 'single_node',
    root_secret VARCHAR(256) NOT NULL,
    version UUID NOT NULL,
    "ipsv4" VARCHAR(15) ARRAY,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    FOREIGN KEY (version) REFERENCES s3_versions(uuid)
);

CREATE INDEX ON s3_instances(project_id, name);
""",
            """\
CREATE TABLE s3_buckets (
    uuid UUID PRIMARY KEY,
    name VARCHAR(63) NOT NULL,
    description TEXT,
    project_id UUID NOT NULL,
    status VARCHAR(64) NOT NULL DEFAULT 'ACTIVE',
    instance UUID NOT NULL,
    versioning_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    quota_bytes BIGINT NOT NULL DEFAULT 0,
    object_lock_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    public BOOLEAN NOT NULL DEFAULT FALSE,
    default_retention_mode VARCHAR(32),
    default_retention_days INT,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    FOREIGN KEY (instance) REFERENCES s3_instances(uuid)
);

CREATE INDEX IF NOT EXISTS s3_buckets_project_id_idx
    ON s3_buckets (project_id);
""",
            """\
CREATE TABLE s3_policies (
    uuid UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    project_id UUID NOT NULL,
    status VARCHAR(64) NOT NULL DEFAULT 'ACTIVE',
    instance UUID NOT NULL,
    content JSONB NOT NULL,
    builtin BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    FOREIGN KEY (instance) REFERENCES s3_instances(uuid)
);

CREATE INDEX IF NOT EXISTS s3_policies_project_id_idx
    ON s3_policies (project_id);
""",
            """\
CREATE TABLE s3_users (
    uuid UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    project_id UUID NOT NULL,
    status VARCHAR(64) NOT NULL DEFAULT 'ACTIVE',
    instance UUID NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    FOREIGN KEY (instance) REFERENCES s3_instances(uuid)
);

CREATE INDEX IF NOT EXISTS s3_users_project_id_idx
    ON s3_users (project_id);
""",
            """\
CREATE TABLE s3_access_keys (
    uuid UUID PRIMARY KEY,
    name VARCHAR(255),
    description TEXT,
    project_id UUID NOT NULL,
    instance UUID NOT NULL,
    status VARCHAR(64) NOT NULL DEFAULT 'ACTIVE',
    "user" UUID NOT NULL,
    access_key VARCHAR(128) NOT NULL UNIQUE,
    secret_key VARCHAR(256) NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    FOREIGN KEY (instance) REFERENCES s3_instances(uuid),
    FOREIGN KEY ("user") REFERENCES s3_users(uuid)
);

CREATE INDEX IF NOT EXISTS s3_access_keys_project_id_idx
    ON s3_access_keys (project_id);
CREATE UNIQUE INDEX IF NOT EXISTS s3_access_keys_access_key_idx
    ON s3_access_keys (access_key);
""",
            """\
CREATE TABLE s3_user_policy_attachments (
    uuid UUID PRIMARY KEY,
    name VARCHAR(255),
    description TEXT,
    project_id UUID NOT NULL,
    instance UUID NOT NULL,
    "user" UUID NOT NULL,
    policy UUID NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    FOREIGN KEY (instance) REFERENCES s3_instances(uuid),
    FOREIGN KEY ("user") REFERENCES s3_users(uuid),
    FOREIGN KEY (policy) REFERENCES s3_policies(uuid)
);

CREATE INDEX IF NOT EXISTS s3_user_policy_attachments_project_id_idx
    ON s3_user_policy_attachments (project_id);
CREATE UNIQUE INDEX IF NOT EXISTS s3_user_policy_attachments_user_policy_idx
    ON s3_user_policy_attachments ("user", policy);
""",
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):

        tables = [
            "s3_user_policy_attachments",
            "s3_access_keys",
            "s3_users",
            "s3_policies",
            "s3_buckets",
            "s3_instances",
            "s3_versions",
        ]

        for table in tables:
            self._delete_table_if_exists(session, table)


migration_step = MigrationStep()
