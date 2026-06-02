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

import logging
import typing as tp
import uuid
import uuid as sys_uuid

from gcl_looper.services.oslo import base as oslo_base
from gcl_sdk.agents.universal.dm import models as ua_models
from gcl_sdk.paas.services import builder

from exordos_paas_s3 import paas_models as models

LOG = logging.getLogger(__name__)
AGENT_UUID5_NAME = "s3aas"


class PaaSBuilder(builder.PaaSBuilder):
    @classmethod
    def agent_uuid_by_node(cls, node_uuid: sys_uuid.UUID) -> sys_uuid.UUID:
        return sys_uuid.uuid5(node_uuid, AGENT_UUID5_NAME)

    def schedule_paas_objects(
        self,
        instance: ua_models.InstanceWithDerivativesMixin,
        paas_objects: tp.Collection[ua_models.TargetResourceKindAwareMixin],
    ) -> dict[sys_uuid.UUID, tp.Collection[ua_models.TargetResourceKindAwareMixin]]:
        """Schedule the PaaS objects.

        The method schedules the PaaS objects. The result is a dictionary
        where the key is a UUID of an agent and the value is a list of PaaS
        objects that should be scheduled on this agent.
        """
        scheduled = {}
        for entity in paas_objects:
            # Entity's uuid is the same as agent's uuid
            scheduled[entity.uuid] = [entity]
        return scheduled


class S3InstanceBuilder(PaaSBuilder, oslo_base.OsloConfigurableService):
    def __init__(
        self,
        instance_model: tp.Type[models.S3Instance] = models.S3Instance,
    ):
        super().__init__(instance_model)

    def _get_buckets(self, instance):
        return {
            b.name: {
                "versioning_enabled": b.versioning_enabled,
                "quota_bytes": b.quota_bytes,
                "object_lock_enabled": b.object_lock_enabled,
                "public": b.public,
                "default_retention_mode": b.default_retention_mode,
                "default_retention_days": b.default_retention_days,
            }
            for b in instance.get_buckets()
        }

    def _get_policies(self, instance):
        return {
            str(p.uuid): {
                "name": p.name,
                "content": p.content,
            }
            for p in instance.get_policies()
        }

    def _get_users(self, instance):
        result = {}
        for u in instance.get_users():
            policies = u.get_policies()
            result[u.name] = {
                "uuid": str(u.uuid),
                "policies": {
                    str(p.uuid): {
                        "name": p.name,
                        "content": p.content,
                    }
                    for p in policies
                },
            }
        return result

    def _get_access_keys(self, instance):
        result = {}
        for u in instance.get_users():
            for key in u.get_access_keys():
                result[key.access_key] = {
                    "secret_key": key.secret_key,
                    "user_name": u.name,
                    "status": key.status,
                }
        return result

    def create_paas_objects(
        self, instance: models.S3Instance
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        """Create a list of PaaS objects."""
        return self.actualize_paas_objects(
            instance, builder.PaaSCollection(paas_objects=tuple())
        )

    def actualize_paas_objects(
        self,
        instance: models.S3Instance,
        paas_collection: builder.PaaSCollection,
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        """Basic update, all derivatives are non-unique."""
        actual_resources = []

        buckets = self._get_buckets(instance)
        policies = self._get_policies(instance)
        users = self._get_users(instance)
        access_keys = self._get_access_keys(instance)

        nodeset = instance.get_actual_nodeset()
        nodes_by_idx = list(nodeset.nodes.keys())

        # Create S3InstanceNode for each node in the cluster
        for i in range(instance.nodes_number):
            actual_resources.append(
                models.S3InstanceNode(
                    uuid=PaaSBuilder.agent_uuid_by_node(uuid.UUID(nodes_by_idx[i])),
                    name=instance.name,
                    instance=instance,
                    buckets=buckets,
                    users=users,
                    policies=policies,
                    access_keys=access_keys,
                )
            )

        return actual_resources
