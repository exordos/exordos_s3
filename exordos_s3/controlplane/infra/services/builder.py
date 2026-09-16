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

from exordos_s3 import constants as c
from exordos_s3.controlplane import cluster
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
RUSTFS_VOLUMES={volumes}
RUSTFS_OBS_LOGGER_LEVEL=error
"""

RUSTFS_ROOT_USER = "admin"


def freeze_members(
    members: dict[str, dict],
    nodes: dict[str, dict],
    nodes_number: int,
) -> dict[str, dict]:
    """Return the members of a distributed instance.

    Ordinals are assigned once, when every node of the set has an address,
    and never move afterwards: the RustFS endpoint list is built from them and
    a drive must keep its position in the erasure set. Later calls only
    refresh the addresses.
    """
    if not members:
        if len(nodes) < nodes_number or not all(n.get("ipv4") for n in nodes.values()):
            return {}
        return {
            node_uuid: {"ordinal": ordinal, "ipv4": nodes[node_uuid]["ipv4"]}
            for ordinal, node_uuid in enumerate(sorted(nodes)[:nodes_number], start=1)
        }

    frozen = {}
    for node_uuid, member in members.items():
        ipv4 = nodes.get(node_uuid, {}).get("ipv4")
        if ipv4 is None:
            ipv4 = member["ipv4"]
        frozen[node_uuid] = {"ordinal": member["ordinal"], "ipv4": ipv4}
    return frozen


def node_addresses(nodes: dict[str, dict]) -> list[str]:
    """Return the addresses of the nodes that have one.

    A node joins its set before its port does, so its entry carries no
    address for a while.
    """
    return [node["ipv4"] for node in nodes.values() if node.get("ipv4")]


def render_rustfs_env(
    root_secret: str,
    members: dict[str, dict] | None = None,
    parity: int | None = None,
) -> str:
    """Render rustfs.env, the same for every node of an instance."""
    if not members:
        return RUSTFS_CONF_TEMPLATE.format(
            root_user=RUSTFS_ROOT_USER,
            root_secret=root_secret,
            volumes=c.RUSTFS_DATA_DIR,
        )

    ordered = sorted(members.values(), key=lambda m: m["ordinal"])
    # A single ellipsis pattern, not a list of hosts: RustFS can only append
    # pools to a deployment whose every pool is written this way.
    host_pattern = c.CLUSTER_HOST_TEMPLATE.format(ordinal=f"{{1...{len(ordered)}}}")
    volumes = f"http://{host_pattern}:{c.RUSTFS_PORT}{c.RUSTFS_DATA_DIR}"
    hosts = ",".join(
        f"{m['ipv4']}={c.CLUSTER_HOST_TEMPLATE.format(ordinal=m['ordinal'])}"
        for m in ordered
    )

    content = RUSTFS_CONF_TEMPLATE.format(
        root_user=RUSTFS_ROOT_USER,
        root_secret=root_secret,
        volumes=volumes,
    )
    content += f"EXORDOS_S3_HOSTS={hosts}\n"
    if parity is not None:
        content += f"RUSTFS_STORAGE_CLASS_STANDARD=EC:{parity}\n"
    return content


class CoreInfraBuilder(builder.CoreInfraBuilder):
    def __init__(
        self,
        core_username: str,
        core_password: str,
        core_api_base_url: str,
        project_id: sys_uuid.UUID,
        instance_model: type[models.S3Instance] = models.S3Instance,
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

        addresses = node_addresses(nodeset_actual.nodes)
        if addresses:
            instance.ipsv4 = addresses

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

        # Handle node shrink
        if instance.nodes_number < len(addresses):
            addresses = addresses[: instance.nodes_number]
            for idx, del_node_uuid in enumerate(nodeset_actual.nodes.keys()):
                if idx < instance.nodes_number:
                    continue
                for key in ua_models.NodeEncryptionKey.objects.get_all(
                    filters={"uuid": dm_filters.EQ(del_node_uuid)}
                ):
                    key.delete()

        config_nodes: tp.Iterable[str] = nodeset_actual.nodes
        members = None
        if instance.is_distributed():
            members = freeze_members(
                instance.members, nodeset_actual.nodes, instance.nodes_number
            )
            # Every node has to know all of its peers from the very first
            # start, otherwise it formats its drive for a different layout.
            if not members:
                instance.status = sdk_c.InstanceStatus.IN_PROGRESS.value
                return (nodeset_target,)
            if members != instance.members:
                instance.members = members
            drift = cluster.membership_drift(members, nodeset_actual.nodes)
            if drift:
                LOG.error(
                    "Node set of instance %s no longer matches its frozen "
                    "membership: %s. The nodes it still has keep serving; a "
                    "replacement node cannot join on its own, because an "
                    "ordinal names a drive position in the erasure set.",
                    instance.uuid,
                    ", ".join(sorted(drift)),
                )
            instance.ipsv4 = [
                m["ipv4"] for m in sorted(members.values(), key=lambda m: m["ordinal"])
            ]
            config_nodes = members

        content = render_rustfs_env(instance.root_secret, members, instance.parity)

        # Recreate configs for each node
        new_configs = []
        for node_uuid_str in config_nodes:
            config = instance._create_config(
                sys_uuid.UUID(node_uuid_str), self._project_id, content
            )
            new_configs.append(config)

        if instance.is_distributed():
            # An ACTIVE node set only means the VMs are up; the cluster is up
            # once the nodes report their RustFS serves requests.
            status = cluster.instance_status(
                instance.nodes_number,
                members,
                nodeset_actual.status,
                nodeset_actual.nodes,
            )
            if status is not None:
                instance.status = status
        else:
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
