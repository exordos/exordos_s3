#    Copyright 2026 Genesis Corporation.
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
"""What the nodes of a cluster say about it, and what that makes it.

Both builders write the status of the same instance -- the infra one whenever
it walks the instance, the paas one when a node reports something new -- so
the verdict lives here. Two verdicts drawn from different sources would undo
each other on every pass.
"""

import logging
import typing as tp
import uuid as sys_uuid

from gcl_sdk.agents.universal.dm import models as ua_models
from gcl_sdk.infra import constants as pc
from restalchemy.dm import filters as dm_filters

LOG = logging.getLogger(__name__)

NODE_KIND = "s3_instance_node"
AGENT_UUID5_NAME = "s3aas"
# The node of a distributed instance that applies IAM and buckets
RECONCILER_ORDINAL = 1


def agent_uuid_by_node(node_uuid: sys_uuid.UUID) -> sys_uuid.UUID:
    return sys_uuid.uuid5(node_uuid, AGENT_UUID5_NAME)


def membership_drift(members: dict[str, dict], nodes: dict[str, dict]) -> set[str]:
    """Return the nodes the frozen membership and the node set disagree on.

    An ordinal names an endpoint of the RustFS pool and a drive position in
    its erasure set, so a replacement node cannot simply take a free one: the
    instance needs an operator rather than another reconciliation cycle.
    """
    return set(members) ^ set(nodes)


def readiness(members: dict[str, dict]) -> list[bool] | None:
    """What each node of the instance last reported about its RustFS.

    `None` when a node has never reported: the cluster cannot be judged on a
    node the control plane has not heard from.
    """
    agents = {agent_uuid_by_node(sys_uuid.UUID(node)) for node in members}
    if not agents:
        return None

    resources = ua_models.Resource.objects.get_all(
        filters={
            "uuid": dm_filters.In(tuple(agents)),
            "kind": dm_filters.EQ(NODE_KIND),
        },
    )
    if len(resources) < len(agents):
        LOG.debug(
            "Only %s of %s cluster nodes have reported so far",
            len(resources),
            len(agents),
        )
        return None
    return [bool(r.value.get("ready")) for r in resources]


def cluster_status(
    nodes_number: int,
    ready_flags: tp.Collection[bool] | None,
) -> str | None:
    """Return the instance status a cluster of these nodes is in.

    A node set goes ACTIVE once its VMs are up, which is a minute or two
    before RustFS serves anything, so the instance follows what the nodes
    report about their RustFS instead.

    `None` means the nodes did not say enough to judge; the caller leaves the
    status as it is. That covers the node whose RustFS stopped serving, too:
    it cannot read the state it is supposed to report, so it reports nothing
    at all and the cluster keeps the verdict it had.
    """
    if ready_flags is None or len(ready_flags) < nodes_number:
        return None

    ready = sum(1 for flag in ready_flags if flag)
    if ready == nodes_number:
        return pc.InstanceStatus.ACTIVE.value
    LOG.debug("%s of %s cluster nodes are ready", ready, nodes_number)
    return pc.InstanceStatus.IN_PROGRESS.value


def instance_status(
    nodes_number: int,
    members: dict[str, dict],
    nodeset_status: str,
    nodeset_nodes: dict[str, dict],
) -> str | None:
    """Return the status of a distributed instance, or `None` to keep it.

    Membership drift is an ERROR whatever the nodes report: the report of a
    node that left the set stays behind. Otherwise a node set that is not
    ACTIVE speaks for the instance, and an ACTIVE one defers to RustFS.
    """
    if not members:
        return pc.InstanceStatus.IN_PROGRESS.value
    if membership_drift(members, nodeset_nodes):
        return pc.InstanceStatus.ERROR.value
    try:
        status = pc.InstanceStatus(nodeset_status).value
    except ValueError:
        return pc.InstanceStatus.IN_PROGRESS.value
    if status != pc.InstanceStatus.ACTIVE.value:
        return status
    return cluster_status(nodes_number, readiness(members))
