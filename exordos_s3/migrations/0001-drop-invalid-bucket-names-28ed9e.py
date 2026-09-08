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

import typing as tp

from restalchemy.storage.sql import migrations


class MigrationStep(migrations.AbstractMigrationStep):
    """Drop bucket rows whose name the dataplane cannot accept.

    S3Bucket.name only had a length check, so names no S3 implementation
    accepts (``world_bucket``) reached the table.  They now fail validation,
    which restalchemy also runs when a row is read back, so leaving them would
    break every read of the owning instance rather than just that one bucket.

    Deleting them loses nothing: the dataplane answered InvalidBucketName for
    exactly these names, so no bucket was ever created and no object can be
    stored under one.  The rule below is the one BucketNameType applies -- the
    two have to stay in step.
    """

    def __init__(self) -> None:
        self._depends = ["0000-init-s3.py"]

    @property
    def migration_id(self) -> str:
        return "28ed9ebb-9971-471a-a613-d1efc92cb5a3"

    @property
    def is_manual(self) -> bool:
        return False

    def upgrade(self, session: tp.Any) -> None:
        session.execute(
            """\
DELETE FROM s3_buckets
WHERE char_length(name) NOT BETWEEN 3 AND 63
   OR name !~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$'
   OR name ~ '^[0-9]{1,3}(\\.[0-9]{1,3}){3}$';
"""
        )

    def downgrade(self, session: tp.Any) -> None:
        # The rows are gone and the buckets they described never existed on a
        # dataplane, so there is nothing to put back.
        pass


migration_step = MigrationStep()
