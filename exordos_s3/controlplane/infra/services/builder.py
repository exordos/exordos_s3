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

import logging
import typing as tp
import uuid as sys_uuid

from gcl_sdk.agents.universal.dm import models as ua_models
from gcl_sdk.agents.universal.drivers import core as core_drivers
from gcl_sdk.infra import constants as sdk_c
from gcl_sdk.infra.dm import models as sdk_models
from gcl_sdk.infra.services import builder
from restalchemy.dm import filters as dm_filters

from exordos_s3.controlplane.infra.dm import models

LOG = logging.getLogger(__name__)
NODE_KIND = sdk_models.Node.get_resource_kind()
NODE_SET_KIND = sdk_models.NodeSet.get_resource_kind()
CONFIG_KIND = sdk_models.Config.get_resource_kind()


RUSTFS_CONF_TEMPLATE = """\
# RustFS node environment configuration
# Managed by Exordos S3 control plane — do not edit manually
RUSTFS_ACCESS_KEY={root_user}
RUSTFS_SECRET_KEY={root_secret}
RUSTFS_ADDRESS=0.0.0.0:9000
RUSTFS_CONSOLE_ADDRESS=127.0.0.1:9001
RUSTFS_CONSOLE_ENABLE=true
RUSTFS_VOLUMES=/var/lib/rustfs/data
RUSTFS_OBS_LOGGER_LEVEL=error
"""


class CoreInfraBuilder(builder.CoreInfraBuilder):
    def __init__(
        self,
        core_username: str,
        core_password: str,
        core_api_base_url: str,
        project_id: sys_uuid.UUID,
        instance_model: tp.Type[models.S3Instance] = models.S3Instance,
    ):
        super().__init__(instance_model)
        self._project_id = project_id
        self.core_driver = core_drivers.RestCoreCapabilityDriver(
            username=core_username,
            password=core_password,
            user_api_base_url=core_api_base_url,
            project_id=self._project_id,
            use_project_scope=True,
            node_set="/v1/compute/sets/",
            config="/v1/config/configs/",
        )
        self._cclient = self.core_driver._client._client

    def create_infra(
        self, instance: models.S3Instance
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        return self.actualize_infra(instance, builder.InfraCollection(infra_objects=()))

    def actualize_infra(
        self,
        instance: models.S3Instance,
        infra: builder.InfraCollection,
    ) -> tp.Collection[ua_models.TargetResourceKindAwareMixin]:
        nodeset_target = None
        nodeset_actual = None

        for target, actual in infra.infra_objects:
            if target.get_resource_kind() == NODE_SET_KIND:
                nodeset_target = target
                nodeset_actual = actual

        # Bootstrap: no NodeSet target yet — create it from instance spec
        if nodeset_target is None:
            for obj in instance.get_infra(self._project_id):
                if obj.get_resource_kind() == NODE_SET_KIND:
                    nodeset_target = obj
                    break
            instance.status = sdk_c.InstanceStatus.IN_PROGRESS.value
            return (nodeset_target,) if nodeset_target is not None else ()

        # Keep NodeSet target in sync with current instance spec
        nodeset_target.cores = instance.cpu
        nodeset_target.ram = instance.ram
        nodeset_target.disk_spec = sdk_models.SetDisksSpec(
            disks=[
                {
                    "size": models.ROOT_DISK_SIZE,
                    "image": instance.version.image,
                    "label": "root",
                },
                {
                    "size": instance.disk_size,
                    "label": "data",
                },
            ]
        )
        nodeset_target.replicas = instance.nodes_number

        # Actual NodeSet not yet provisioned
        if nodeset_actual is None:
            instance.status = sdk_c.InstanceStatus.IN_PROGRESS.value
            return (nodeset_target,)

        if nodeset_actual.nodes:
            instance.ipsv4 = [node["ipv4"] for node in nodeset_actual.nodes.values()]

        # Sync private keys for DP nodes into local DB
        node_keys = self._cclient.do_action(
            "/v1/compute/sets/", "get_private_keys", nodeset_actual.uuid
        )
        for u, v in node_keys.items():
            if nkey := ua_models.NodeEncryptionKey.objects.get_one_or_none(
                filters={"uuid": dm_filters.EQ(u)}
            ):
                nkey.private_key = v
                nkey.update()
            else:
                nkey = ua_models.NodeEncryptionKey(uuid=sys_uuid.UUID(u), private_key=v)
                nkey.insert()

        node_addresses = [node["ipv4"] for node in nodeset_actual.nodes.values()]

        # Handle node shrink
        if instance.nodes_number < len(node_addresses):
            node_addresses = node_addresses[: instance.nodes_number]
            for idx, del_node_uuid in enumerate(nodeset_actual.nodes.keys()):
                if idx < instance.nodes_number:
                    continue
                for key in ua_models.NodeEncryptionKey.objects.get_all(
                    filters={"uuid": dm_filters.EQ(del_node_uuid)}
                ):
                    key.delete()

        # Recreate configs for each node
        new_configs = []
        for node_uuid_str, _ in nodeset_actual.nodes.items():
            content = RUSTFS_CONF_TEMPLATE.format(
                root_user="admin",
                root_secret=instance.root_secret,
            )
            config = instance._create_config(
                sys_uuid.UUID(node_uuid_str), self._project_id, content
            )
            new_configs.append(config)

        try:
            instance.status = sdk_c.InstanceStatus(nodeset_actual.status).value
        except ValueError:
            instance.status = sdk_c.InstanceStatus.IN_PROGRESS.value

        return (nodeset_target, *new_configs)

    def pre_delete_instance_resource(self, resource):
        # Get actual nodeset to clean private keys of its nodes
        target_resources = ua_models.TargetResource.objects.get_all(
            filters={
                "master": dm_filters.EQ(resource.uuid),
                "kind": dm_filters.EQ(NODE_SET_KIND),
            },
        )
        actual_resources = ua_models.Resource.objects.get_all(
            filters={
                "uuid": dm_filters.In(r.uuid for r in target_resources),
                "kind": dm_filters.EQ(NODE_SET_KIND),
            },
        )

        for ns in actual_resources:
            for key in ua_models.NodeEncryptionKey.objects.get_all(
                filters={"uuid": dm_filters.In(ns.value["nodes"].keys())}
            ):
                key.delete()
