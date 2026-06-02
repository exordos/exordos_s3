#    Copyright 2026 Genesis Corporation.
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

import os

from exordos_metapaas.registry import PaaSDefinition
from exordos_paas_s3 import permissions
from exordos_paas_s3 import routes


class S3Definition(PaaSDefinition):
    """S3aaS as a metapaas plugin: control-plane API, dataplane (rustfs) and
    all the runtime wiring (builders, core-agent models, IAM perms) declared
    through the registry contract so the metapaas runtime hosts it generically.
    """

    slug = "s3"
    element_name = "s3aas"

    def get_type_route(self):
        return routes.S3Route

    def get_migrations_path(self):
        return os.path.join(os.path.dirname(__file__), "migrations")

    def get_builders(self):
        return [
            {
                "service": "exordos_paas_s3.infra_builder:CoreInfraBuilder",
                "core_creds": True,
            },
            {
                "service": "exordos_paas_s3.paas_builder:S3InstanceBuilder",
                "core_creds": False,
            },
        ]

    def get_agent_models(self):
        # Keys are resource sub-paths under types.s3; the runtime prefixes them
        # with em_<element>_types_s3_ to match the element-manager target kind.
        return {
            "versions": "exordos_paas_s3.models:S3Version",
            "instances": "exordos_paas_s3.infra_models:S3Instance",
            "instances.buckets": "exordos_paas_s3.models:S3Bucket",
            "instances.policies": "exordos_paas_s3.models:S3Policy",
            "instances.users": "exordos_paas_s3.models:S3User",
            "instances.users.access_keys": "exordos_paas_s3.models:S3AccessKey",
            "instances.users.policies": "exordos_paas_s3.models:S3UserPolicyAttachment",
        }

    def get_agent_filters(self):
        # versions are shared across the project (scoped by description carrying
        # the project id); everything else scopes by project_id.
        return {
            "versions": "description",
            "instances": "project_id",
            "instances.buckets": "project_id",
            "instances.policies": "project_id",
            "instances.users": "project_id",
            "instances.users.access_keys": "project_id",
            "instances.users.policies": "project_id",
        }

    def get_iam_permissions(self):
        return list(permissions.PERMS_OWNER)
