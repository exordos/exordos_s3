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
import re
import secrets
import string
import typing as tp

from gcl_sdk.agents.universal.dm import models as ua_models
from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import relationships
from restalchemy.dm import types
from restalchemy.storage.sql import orm

from exordos_s3 import utils as u

# Lengths and alphabets for S3 credential generation
ACCESS_KEY_LENGTH = 20
SECRET_KEY_LENGTH = 40
ROOT_SECRET_LENGTH = 64
ACCESS_KEY_ALPHABET = string.ascii_letters + string.digits
SECRET_KEY_ALPHABET = string.ascii_letters + string.digits
ROOT_SECRET_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*"


# S3 bucket names must be DNS compatible: the dataplane rejects anything else
# with InvalidBucketName, so bad names are refused here instead of wedging
# reconciliation of the whole instance.
#
# Only the rules every S3 implementation enforces are checked.  The type also
# runs when a row is read back, and real installations hold no bucket rows that
# fail it -- keeping the check to what the dataplane itself refuses means such
# a row could never have had a bucket behind it in the first place.
BUCKET_NAME_MIN_LENGTH = 3
BUCKET_NAME_MAX_LENGTH = 63


class BucketNameType(types.BaseCompiledRegExpTypeFromAttr):
    pattern = re.compile(
        # Not shaped like an IPv4 address.
        r"(?!\d{1,3}(?:\.\d{1,3}){3}\Z)"
        # Dot separated labels of lowercase letters, digits and hyphens; every
        # label starts and ends with a letter or a digit.
        r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\Z"
    )

    def validate(self, value: tp.Any) -> bool:
        if not isinstance(value, str):
            return False
        if not BUCKET_NAME_MIN_LENGTH <= len(value) <= BUCKET_NAME_MAX_LENGTH:
            return False
        return super().validate(value)

    @property
    def example(self) -> str:
        return "my-bucket"


class S3ValidationError(ra_exc.ValidationErrorException):
    """The request contradicts the model: a client error, not a failure."""

    message = "%(details)s"


class S3Status(str, enum.Enum):
    NEW = "NEW"
    IN_PROGRESS = "IN_PROGRESS"
    ACTIVE = "ACTIVE"
    ERROR = "ERROR"


class S3InstanceKind(str, enum.Enum):
    SINGLE_NODE = "single_node"
    DISTRIBUTED = "distributed"


# RustFS docs require at least four servers for a distributed deployment, and a
# single erasure set holds at most 16 drives (one drive per node here).
DISTRIBUTED_MIN_NODES = 4
DISTRIBUTED_MAX_NODES = 16

# The layout of a distributed instance is fixed at creation: RustFS cannot
# change the drive count of a pool, and parity only applies to new objects.
IMMUTABLE_FIELDS = ("kind", "nodes_number", "parity")
# Changing these reboots every node of the set at once.
DISTRIBUTED_IMMUTABLE_FIELDS = ("cpu", "ram")


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
        default=list,
    )
    cpu = properties.property(types.Integer(min_value=1, max_value=128))
    ram = properties.property(types.Integer(min_value=512, max_value=1024**3))
    disk_size = properties.property(types.Integer(min_value=8, max_value=1024**3))
    nodes_number = properties.property(types.Integer(min_value=1, max_value=16))
    kind = properties.property(
        types.Enum([k.value for k in S3InstanceKind]),
        default=S3InstanceKind.SINGLE_NODE.value,
    )
    # Erasure-coding parity (RUSTFS_STORAGE_CLASS_STANDARD=EC:<parity>) of a
    # distributed instance; None keeps the RustFS default for the set size.
    parity = properties.property(
        types.AllowNone(
            types.Integer(min_value=1, max_value=DISTRIBUTED_MAX_NODES // 2)
        ),
        default=None,
    )
    # Nodes of a distributed instance by node uuid: {"ordinal": n, "ipv4": ip}.
    # Ordinals are assigned once and name the RustFS endpoints, so they must
    # not follow the order of the node set.
    members = properties.property(types.Dict(), default=dict)
    root_secret = properties.property(
        types.String(min_length=1, max_length=256),
        default=lambda: "".join(
            secrets.choice(ROOT_SECRET_ALPHABET) for _ in range(ROOT_SECRET_LENGTH)
        ),
    )
    version = relationships.relationship(S3Version, required=True, read_only=True)

    def is_distributed(self) -> bool:
        return self.kind == S3InstanceKind.DISTRIBUTED.value

    def _validate_kind(self):
        if self.kind == S3InstanceKind.SINGLE_NODE.value:
            if self.nodes_number != 1:
                raise S3ValidationError(
                    details="single_node kind requires nodes_number=1"
                )
            if self.parity is not None:
                raise S3ValidationError(
                    details="parity is supported only by the distributed kind"
                )
            return

        if not DISTRIBUTED_MIN_NODES <= self.nodes_number <= DISTRIBUTED_MAX_NODES:
            raise S3ValidationError(
                details=(
                    f"distributed kind requires nodes_number between "
                    f"{DISTRIBUTED_MIN_NODES} and {DISTRIBUTED_MAX_NODES}"
                )
            )
        if self.parity is not None and self.parity > self.nodes_number // 2:
            raise S3ValidationError(details="parity can't exceed half of nodes_number")

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
            raise S3ValidationError(details="disk_size shrink is not supported yet")

        immutable = IMMUTABLE_FIELDS
        if self.is_distributed():
            immutable += DISTRIBUTED_IMMUTABLE_FIELDS
        for name in immutable:
            if self.properties[name].is_dirty():
                raise S3ValidationError(details=f"{name} can't be changed")

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

    name = properties.property(BucketNameType(), required=True, read_only=True)
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
