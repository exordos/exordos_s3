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

PERMS_OWNER = [
    "genesis_s3.s3_instance.create",
    "genesis_s3.s3_instance.read",
    "genesis_s3.s3_instance.update",
    "genesis_s3.s3_instance.delete",
    "genesis_s3.bucket.create",
    "genesis_s3.bucket.read",
    "genesis_s3.bucket.update",
    "genesis_s3.bucket.delete",
    "genesis_s3.policy.create",
    "genesis_s3.policy.read",
    "genesis_s3.policy.update",
    "genesis_s3.policy.delete",
    "genesis_s3.user.create",
    "genesis_s3.user.read",
    "genesis_s3.user.update",
    "genesis_s3.user.delete",
    "genesis_s3.access_key.create",
    "genesis_s3.access_key.read",
    "genesis_s3.access_key.update",
    "genesis_s3.access_key.delete",
    "genesis_s3.s3_version.read",
]

ALL_PERMS = set(PERMS_OWNER)

ROLES = {
    "owner": PERMS_OWNER,
}
