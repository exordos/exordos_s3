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
    "exordos_s3.s3_instance.create",
    "exordos_s3.s3_instance.read",
    "exordos_s3.s3_instance.update",
    "exordos_s3.s3_instance.delete",
    "exordos_s3.bucket.create",
    "exordos_s3.bucket.read",
    "exordos_s3.bucket.update",
    "exordos_s3.bucket.delete",
    "exordos_s3.policy.create",
    "exordos_s3.policy.read",
    "exordos_s3.policy.update",
    "exordos_s3.policy.delete",
    "exordos_s3.user.create",
    "exordos_s3.user.read",
    "exordos_s3.user.update",
    "exordos_s3.user.delete",
    "exordos_s3.access_key.create",
    "exordos_s3.access_key.read",
    "exordos_s3.access_key.update",
    "exordos_s3.access_key.delete",
    "exordos_s3.s3_version.read",
]

ALL_PERMS = set(PERMS_OWNER)

ROLES = {
    "owner": PERMS_OWNER,
}
