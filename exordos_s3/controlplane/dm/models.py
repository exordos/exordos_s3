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

import enum
import secrets
import string

from gcl_sdk.agents.universal.dm import models as ua_models
from restalchemy.dm import filters as dm_filters
from restalchemy.dm import models, properties, relationships, types
from restalchemy.storage.sql import orm

from exordos_s3 import utils as u

# Lengths and alphabets for S3 credential generation
ACCESS_KEY_LENGTH = 20
SECRET_KEY_LENGTH = 40
ROOT_SECRET_LENGTH = 64
ACCESS_KEY_ALPHABET = string.ascii_letters + string.digits
SECRET_KEY_ALPHABET = string.ascii_letters + string.digits
ROOT_SECRET_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*"


class S3Status(str, enum.Enum):
    NEW = "NEW"
    IN_PROGRESS = "IN_PROGRESS"
    ACTIVE = "ACTIVE"
    ERROR = "ERROR"


class S3InstanceKind(str, enum.Enum):
    SINGLE_NODE = "single_node"
    # Future: DISTRIBUTED = "distributed"


class S3RetentionMode(str, enum.Enum):
    GOVERNANCE = "GOVERNANCE"
    COMPLIANCE = "COMPLIANCE"


class S3Version(
    models.ModelWithUUID,
    models.ModelWithNameDesc,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
    ua_models.TargetResourceMixin,
):
    __tablename__ = "s3_versions"

    image = properties.property(types.String(max_length=2048))


class S3Instance(
    models.ModelWithUUID,
    models.ModelWithNameDesc,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "s3_instances"

    name = properties.property(types.String(min_length=1, max_length=255))
    status = properties.property(
        types.Enum([status.value for status in S3Status]),
        default=S3Status.NEW.value,
    )
    ipsv4 = properties.property(
        types.TypedList(types.String(max_length=15)),
        default=lambda: [],
    )
    cpu = properties.property(types.Integer(min_value=1, max_value=128))
    ram = properties.property(types.Integer(min_value=512, max_value=1024**3))
    disk_size = properties.property(types.Integer(min_value=8, max_value=1024**3))
    nodes_number = properties.property(types.Integer(min_value=1, max_value=16))
    kind = properties.property(
        types.Enum([k.value for k in S3InstanceKind]),
        default=S3InstanceKind.SINGLE_NODE.value,
    )
    root_secret = properties.property(
        types.String(min_length=1, max_length=256),
        default=lambda: "".join(
            secrets.choice(ROOT_SECRET_ALPHABET) for _ in range(ROOT_SECRET_LENGTH)
        ),
    )
    version = relationships.relationship(S3Version, required=True, read_only=True)

    def _validate_kind(self):
        if self.kind == S3InstanceKind.SINGLE_NODE.value:
            if self.nodes_number != 1:
                raise ValueError("single_node kind requires nodes_number=1")

    def insert(self, session=None):
        self._validate_kind()
        super().insert(session=session)

    def get_users(self, session=None):
        return S3User.objects.get_all(
            session=session, filters={"instance": dm_filters.EQ(self)}
        )

    def get_buckets(self, session=None):
        return S3Bucket.objects.get_all(
            session=session, filters={"instance": dm_filters.EQ(self)}
        )

    def get_policies(self, session=None):
        return S3Policy.objects.get_all(
            session=session, filters={"instance": dm_filters.EQ(self)}
        )

    def _validate_update(self, session=None):
        disk_size = self.properties["disk_size"]
        if disk_size.is_dirty() and disk_size.old_value > self.disk_size:
            raise ValueError("disk_size shrink is not supported yet")

    def update(self, session=None, force=False):
        self._validate_kind()
        self._validate_update(session=session)
        super().update(session=session, force=force)

    def delete(self, session=None, **kwargs):
        u.remove_nested_dm(S3User, "instance", self, session=session)
        u.remove_nested_dm(S3Bucket, "instance", self, session=session)
        u.remove_nested_dm(S3Policy, "instance", self, session=session)
        return super().delete(session=session, **kwargs)


class InstanceChildModel(
    models.ModelWithUUID,
    models.ModelWithNameDesc,
    models.ModelWithTimestamp,
    models.ModelWithProject,
    ua_models.TargetResourceMixin,
    orm.SQLStorableMixin,
):
    instance = relationships.relationship(S3Instance, required=True, read_only=True)

    def touch_parent(self, session=None):
        # Enforce dataplane updates via parent model
        self.instance.update(force=True)

    def insert(self, session=None):
        super().insert(session=session)
        self.touch_parent(session=session)

    def update(self, session=None, force=False):
        super().update(session=session, force=force)
        self.touch_parent(session=session)

    def delete(self, session=None, **kwargs):
        res = super().delete(session=session, **kwargs)
        self.touch_parent(session=session)
        return res


class S3Bucket(InstanceChildModel):
    __tablename__ = "s3_buckets"

    name = properties.property(
        types.String(min_length=3, max_length=63), required=True, read_only=True
    )
    status = properties.property(
        types.Enum([status.value for status in S3Status]),
        default=S3Status.ACTIVE.value,
    )
    versioning_enabled = properties.property(
        types.Boolean(), default=False, read_only=True
    )
    quota_bytes = properties.property(
        types.Integer(min_value=0, max_value=2**63 - 1), default=0
    )
    object_lock_enabled = properties.property(
        types.Boolean(), default=False, read_only=True
    )
    public = properties.property(types.Boolean(), default=False)
    default_retention_mode = properties.property(
        types.AllowNone(types.Enum([mode.value for mode in S3RetentionMode])),
        default=None,
    )
    default_retention_days = properties.property(
        types.AllowNone(types.Integer(min_value=1, max_value=365000)),
        default=None,
    )


class S3Policy(InstanceChildModel):
    __tablename__ = "s3_policies"

    name = properties.property(
        types.String(min_length=1, max_length=255), required=True
    )
    status = properties.property(
        types.Enum([status.value for status in S3Status]),
        default=S3Status.ACTIVE.value,
    )
    content = properties.property(types.Dict(), required=True)

    def delete(self, session=None, **kwargs):
        u.remove_nested_dm(S3UserPolicyAttachment, "policy", self, session=session)
        return super().delete(session=session, **kwargs)


def _generate_access_key():
    return "".join(
        secrets.choice(ACCESS_KEY_ALPHABET) for _ in range(ACCESS_KEY_LENGTH)
    )


def _generate_secret_key():
    return "".join(
        secrets.choice(SECRET_KEY_ALPHABET) for _ in range(SECRET_KEY_LENGTH)
    )


class S3User(InstanceChildModel):
    __tablename__ = "s3_users"

    name = properties.property(
        types.String(min_length=1, max_length=255), required=True, read_only=True
    )
    status = properties.property(
        types.Enum([status.value for status in S3Status]),
        default=S3Status.ACTIVE.value,
    )

    def get_access_keys(self, session=None):
        return S3AccessKey.objects.get_all(
            session=session, filters={"user": dm_filters.EQ(self)}
        )

    def delete(self, session=None, **kwargs):
        u.remove_nested_dm(S3AccessKey, "user", self, session=session)
        u.remove_nested_dm(S3UserPolicyAttachment, "user", self, session=session)
        return super().delete(session=session, **kwargs)

    def get_policies(self, session=None):
        """Get all policies attached to this user."""
        attachments = S3UserPolicyAttachment.objects.get_all(
            session=session, filters={"user": dm_filters.EQ(self)}
        )
        return [att.policy for att in attachments]


class S3UserPolicyAttachment(
    InstanceChildModel,
):
    __tablename__ = "s3_user_policy_attachments"

    user = relationships.relationship(S3User, required=True)
    policy = relationships.relationship(S3Policy, required=True)


class S3AccessKey(
    InstanceChildModel,
):
    __tablename__ = "s3_access_keys"

    user = relationships.relationship(S3User, required=True, read_only=True)
    access_key = properties.property(
        types.String(min_length=10, max_length=128),
        default=_generate_access_key,
    )
    secret_key = properties.property(
        types.String(min_length=10, max_length=256),
        default=_generate_secret_key,
    )
    status = properties.property(
        types.Enum([status.value for status in S3Status]),
        default=S3Status.ACTIVE.value,
    )

    def touch_instance(self, session=None):
        # Propagate changes up to instance for dataplane sync
        self.user.instance.update(force=True)

    def insert(self, session=None):
        super().insert(session=session)
        self.touch_instance(session=session)

    def update(self, session=None, force=False):
        super().update(session=session, force=force)
        self.touch_instance(session=session)

    def delete(self, session=None, **kwargs):
        res = super().delete(session=session, **kwargs)
        self.touch_instance(session=session)
        return res
