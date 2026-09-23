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
        self._depends = ["0000-init-s3.py"]

    @property
    def migration_id(self):
        return "5d0b8e0e-3f4a-4c1e-9a52-7b6f0c2d9e41"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        expressions = [
            """\
ALTER TABLE s3_instances
    ADD COLUMN IF NOT EXISTS parity INT CHECK (parity BETWEEN 1 AND 8);
""",
            """\
ALTER TABLE s3_instances
    ADD COLUMN IF NOT EXISTS members JSONB NOT NULL DEFAULT '{}';
""",
            # `parity` joins the target fields of the s3_instance_iaas
            # resource, so the rows written before this migration carry a
            # value the model no longer produces. Nudge them to be rebuilt.
            """\
UPDATE s3_instances SET updated_at = now();
""",
        ]

        for expression in expressions:
            session.execute(expression)

    def downgrade(self, session):
        session.execute("ALTER TABLE s3_instances DROP COLUMN IF EXISTS members;")
        session.execute("ALTER TABLE s3_instances DROP COLUMN IF EXISTS parity;")


migration_step = MigrationStep()
