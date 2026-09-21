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
        return "a9aaee01-ffc6-4f34-ad89-a44b336cd9dd"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """\
ALTER TABLE s3_instances
    ADD COLUMN IF NOT EXISTS disk_used_percent INT
        CHECK (disk_used_percent BETWEEN 0 AND 100);
"""
        )

    def downgrade(self, session):
        session.execute(
            "ALTER TABLE s3_instances DROP COLUMN IF EXISTS disk_used_percent;"
        )


migration_step = MigrationStep()
