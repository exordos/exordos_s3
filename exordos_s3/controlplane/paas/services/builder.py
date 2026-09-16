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

from gcl_sdk.agents.universal.dm import models as ua_models
from gcl_sdk.paas.services import builder
from restalchemy.storage import exceptions as storage_exceptions

from exordos_s3.controlplane import cluster
from exordos_s3.controlplane.paas.dm import models

LOG = logging.getLogger(__name__)
AGENT_UUID5_NAME = cluster.AGENT_UUID5_NAME
RECONCILER_ORDINAL = cluster.RECONCILER_ORDINAL


class PaaSBuilder(builder.PaaSBuilder):
    @classmethod
    def agent_uuid_by_node(cls, node_uuid: sys_uuid.UUID) -> sys_uuid.UUID:
        return cluster.agent_uuid_by_node(node_uuid)

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


class S3InstanceBuilder(PaaSBuilder):
    def __init__(
        self,
        instance_model: type[models.S3Instance] = models.S3Instance,
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

    @staticmethod
    def _actualize_cluster_status(instance: models.S3Instance) -> None:
        try:
            nodeset = instance.get_actual_nodeset()
        except storage_exceptions.RecordNotFound:
            return
        status = cluster.instance_status(
            instance.nodes_number, instance.members, nodeset.status, nodeset.nodes
        )
        if status is not None:
            instance.status = status

    def create_paas_objects(
        self, instance: models.S3Instance
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        """Create a list of PaaS objects."""
        return self.actualize_paas_objects(
            instance, builder.PaaSCollection(paas_objects=())
        )

    def actualize_paas_objects(
        self,
        instance: models.S3Instance,
        paas_collection: builder.PaaSCollection,
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        """Basic update, all derivatives are non-unique."""
        actual_resources = []

        # The DP-report and API paths both pass here, so the status follows
        # RustFS whatever triggered the actualization -- a node changing what
        # it reports, or a field changed through the API. A NodeSet master
        # change is covered by the infra builder instead.
        if instance.is_distributed():
            self._actualize_cluster_status(instance)

        buckets = self._get_buckets(instance)
        policies = self._get_policies(instance)
        users = self._get_users(instance)
        access_keys = self._get_access_keys(instance)

        if instance.is_distributed():
            # Members are frozen by the infra builder once every node has an
            # address; until then there is no cluster to configure.
            members = sorted(
                instance.members.items(), key=lambda item: item[1]["ordinal"]
            )
            return [
                models.S3InstanceNode(
                    uuid=PaaSBuilder.agent_uuid_by_node(uuid.UUID(node_uuid)),
                    name=instance.name,
                    instance=instance,
                    buckets=buckets,
                    users=users,
                    policies=policies,
                    access_keys=access_keys,
                    reconciler=member["ordinal"] == RECONCILER_ORDINAL,
                )
                for node_uuid, member in members
            ]

        try:
            nodeset = instance.get_actual_nodeset()
        except storage_exceptions.RecordNotFound:
            LOG.debug(
                "Nodeset for s3 instance %s not ready yet, skipping", instance.uuid
            )
            return []

        nodes_by_idx = list(nodeset.nodes.keys())
        if not nodes_by_idx:
            return []

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
