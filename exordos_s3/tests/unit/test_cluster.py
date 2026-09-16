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

import uuid as sys_uuid
from unittest import mock


from exordos_s3.controlplane import cluster
from exordos_s3.controlplane.dm import models as dm_models
from exordos_s3.controlplane.infra.services import builder as infra_builder
from gcl_sdk.infra.services import builder as sdk_infra_builder
from gcl_sdk.paas.services import builder as sdk_paas_builder

from exordos_s3.controlplane.paas.services import builder as paas_builder

NODES = {
    "c0000000-0000-0000-0000-000000000000": {"ipv4": "10.0.0.3"},
    "a0000000-0000-0000-0000-000000000000": {"ipv4": "10.0.0.1"},
    "d0000000-0000-0000-0000-000000000000": {"ipv4": "10.0.0.4"},
    "b0000000-0000-0000-0000-000000000000": {"ipv4": "10.0.0.2"},
}


class TestFreezeMembers:
    def test_waits_for_every_node(self) -> None:
        nodes = dict(list(NODES.items())[:3])

        assert infra_builder.freeze_members({}, nodes, 4) == {}

    def test_waits_for_every_address(self) -> None:
        nodes = {**NODES, "d0000000-0000-0000-0000-000000000000": {}}

        assert infra_builder.freeze_members({}, nodes, 4) == {}

    def test_ordinals_follow_node_uuids(self) -> None:
        members = infra_builder.freeze_members({}, NODES, 4)

        assert {u: m["ordinal"] for u, m in members.items()} == {
            "a0000000-0000-0000-0000-000000000000": 1,
            "b0000000-0000-0000-0000-000000000000": 2,
            "c0000000-0000-0000-0000-000000000000": 3,
            "d0000000-0000-0000-0000-000000000000": 4,
        }
        assert members["c0000000-0000-0000-0000-000000000000"]["ipv4"] == "10.0.0.3"

    def test_frozen_ordinals_only_refresh_addresses(self) -> None:
        members = {
            "a0000000-0000-0000-0000-000000000000": {"ordinal": 4, "ipv4": "10.0.0.1"},
            "b0000000-0000-0000-0000-000000000000": {"ordinal": 3, "ipv4": "10.0.0.2"},
            "c0000000-0000-0000-0000-000000000000": {"ordinal": 2, "ipv4": "10.0.0.3"},
            "d0000000-0000-0000-0000-000000000000": {"ordinal": 1, "ipv4": "10.0.0.4"},
        }
        nodes = {**NODES, "a0000000-0000-0000-0000-000000000000": {"ipv4": "10.0.0.9"}}

        frozen = infra_builder.freeze_members(members, nodes, 4)

        assert frozen == {
            **members,
            "a0000000-0000-0000-0000-000000000000": {"ordinal": 4, "ipv4": "10.0.0.9"},
        }

    def test_missing_node_keeps_its_place(self) -> None:
        members = infra_builder.freeze_members({}, NODES, 4)
        nodes = dict(NODES)
        del nodes["b0000000-0000-0000-0000-000000000000"]

        assert infra_builder.freeze_members(members, nodes, 4) == members


class TestRenderRustfsEnv:
    def test_single_node_env_is_unchanged(self) -> None:
        # Existing instances must not see a config change, it restarts RustFS
        assert infra_builder.render_rustfs_env("secret") == (
            "# RustFS node environment configuration\n"
            "# Managed by Exordos S3 control plane — do not edit manually\n"
            "RUSTFS_ACCESS_KEY=admin\n"
            "RUSTFS_SECRET_KEY=secret\n"
            "RUSTFS_ADDRESS=0.0.0.0:9000\n"
            "RUSTFS_CONSOLE_ADDRESS=127.0.0.1:9001\n"
            "RUSTFS_CONSOLE_ENABLE=true\n"
            "RUSTFS_VOLUMES=/var/lib/rustfs/data\n"
            "RUSTFS_OBS_LOGGER_LEVEL=error\n"
        )

    def test_distributed_env(self) -> None:
        members = infra_builder.freeze_members({}, NODES, 4)

        lines = infra_builder.render_rustfs_env("secret", members, 1).splitlines()

        assert (
            "RUSTFS_VOLUMES=http://node{1...4}.rustfs.internal:9000/var/lib/rustfs/data"
            in lines
        )
        assert (
            "EXORDOS_S3_HOSTS=10.0.0.1=node1.rustfs.internal,"
            "10.0.0.2=node2.rustfs.internal,10.0.0.3=node3.rustfs.internal,"
            "10.0.0.4=node4.rustfs.internal"
        ) in lines
        assert "RUSTFS_STORAGE_CLASS_STANDARD=EC:1" in lines

    def test_default_parity_is_left_to_rustfs(self) -> None:
        members = infra_builder.freeze_members({}, NODES, 4)

        env = infra_builder.render_rustfs_env("secret", members)

        assert "RUSTFS_STORAGE_CLASS_STANDARD" not in env


class TestPaaSObjects:
    def _collection(self) -> sdk_paas_builder.PaaSCollection:
        return sdk_paas_builder.PaaSCollection(paas_objects=())

    def _instance(self, members: dict) -> mock.Mock:
        instance = mock.Mock()
        instance.name = "s3"
        instance.members = members
        instance.nodes_number = 4
        instance.parity = 1
        instance.is_distributed.return_value = True
        instance.get_actual_nodeset.return_value = mock.Mock(
            status="ACTIVE", nodes=NODES
        )
        instance.get_buckets.return_value = []
        instance.get_policies.return_value = []
        instance.get_users.return_value = []
        return instance

    def test_one_node_reconciles(self) -> None:
        members = infra_builder.freeze_members({}, NODES, 4)
        builder = paas_builder.S3InstanceBuilder()

        with mock.patch.object(cluster, "readiness", return_value=None):
            nodes = builder.actualize_paas_objects(
                self._instance(members), self._collection()
            )

        reconcilers = [n.uuid for n in nodes if n.reconciler]
        assert len(nodes) == 4
        assert reconcilers == [
            paas_builder.PaaSBuilder.agent_uuid_by_node(
                sys_uuid.UUID("a0000000-0000-0000-0000-000000000000")
            )
        ]

    def test_only_followers_carry_the_flag(self) -> None:
        members = infra_builder.freeze_members({}, NODES, 4)
        builder = paas_builder.S3InstanceBuilder()

        with mock.patch.object(cluster, "readiness", return_value=None):
            nodes = builder.actualize_paas_objects(
                self._instance(members), self._collection()
            )

        for node in nodes:
            fields = node.get_resource_target_fields()
            assert ("reconciler" in fields) is (not node.reconciler)

    def test_no_members_no_nodes(self) -> None:
        builder = paas_builder.S3InstanceBuilder()

        assert (
            builder.actualize_paas_objects(self._instance({}), self._collection()) == []
        )


def test_distributed_kind_value() -> None:
    assert dm_models.S3InstanceKind("distributed").value == "distributed"


class TestClusterStatus:
    def test_active_only_when_every_node_serves(self) -> None:
        assert cluster.cluster_status(4, [True] * 4) == "ACTIVE"

    def test_a_node_that_does_not_serve_holds_the_instance_back(self) -> None:
        assert cluster.cluster_status(4, [True, True, True, False]) == "IN_PROGRESS"

    def test_a_node_the_cp_has_not_heard_from_leaves_the_status_alone(self) -> None:
        # A node whose RustFS stopped serving cannot read the state it is
        # supposed to report, so it reports nothing at all
        assert cluster.cluster_status(4, None) is None

    def test_no_nodes_at_all_leaves_the_status_alone(self) -> None:
        assert cluster.cluster_status(4, []) is None


class TestMembershipDrift:
    def test_no_drift_when_the_node_set_matches(self) -> None:
        members = infra_builder.freeze_members({}, NODES, 4)

        assert cluster.membership_drift(members, NODES) == set()

    def test_replacement_node_is_drift(self) -> None:
        members = infra_builder.freeze_members({}, NODES, 4)
        nodes = dict(NODES)
        del nodes["b0000000-0000-0000-0000-000000000000"]
        nodes["e0000000-0000-0000-0000-000000000000"] = {"ipv4": "10.0.0.5"}

        assert cluster.membership_drift(members, nodes) == {
            "b0000000-0000-0000-0000-000000000000",
            "e0000000-0000-0000-0000-000000000000",
        }


class TestNodeAddresses:
    def test_returns_the_addresses_it_has(self) -> None:
        assert infra_builder.node_addresses(NODES) == [
            "10.0.0.3",
            "10.0.0.1",
            "10.0.0.4",
            "10.0.0.2",
        ]

    def test_a_node_without_a_port_yet_is_skipped(self) -> None:
        nodes = {**NODES, "a0000000-0000-0000-0000-000000000000": {}}

        assert "10.0.0.1" not in infra_builder.node_addresses(nodes)
        assert len(infra_builder.node_addresses(nodes)) == 3


class TestReadiness:
    def _resource(self, ready: bool) -> mock.Mock:
        return mock.Mock(value={"ready": ready})

    def test_reads_what_every_node_reported(self) -> None:
        members = infra_builder.freeze_members({}, NODES, 4)
        resources = [self._resource(True)] * 3 + [self._resource(False)]

        manager = mock.Mock()
        manager.get_all.return_value = resources

        with mock.patch.object(cluster.ua_models.Resource, "objects", manager):
            assert cluster.readiness(members) == [True, True, True, False]

        agents = manager.get_all.call_args.kwargs["filters"]["uuid"].value
        assert set(agents) == {
            cluster.agent_uuid_by_node(sys_uuid.UUID(node)) for node in members
        }

    def test_a_node_that_never_reported_is_not_a_verdict(self) -> None:
        members = infra_builder.freeze_members({}, NODES, 4)

        manager = mock.Mock()
        manager.get_all.return_value = [self._resource(True)] * 3

        with mock.patch.object(cluster.ua_models.Resource, "objects", manager):
            assert cluster.readiness(members) is None

    def test_no_members_no_verdict(self) -> None:
        assert cluster.readiness({}) is None


MEMBERS = infra_builder.freeze_members({}, NODES, 4)
REPLACED = {
    **{u: n for u, n in NODES.items() if not u.startswith("b")},
    "e0000000-0000-0000-0000-000000000000": {"ipv4": "10.0.0.5"},
}


class TestInstanceStatus:
    def _status(self, nodeset_status, nodes, ready_flags, members=MEMBERS):
        with mock.patch.object(cluster, "readiness", return_value=ready_flags):
            return cluster.instance_status(4, members, nodeset_status, nodes)

    def test_no_members_yet_is_in_progress(self) -> None:
        assert self._status("ACTIVE", NODES, [True] * 4, members={}) == "IN_PROGRESS"

    def test_drift_is_an_error_whatever_the_nodes_reported(self) -> None:
        # The node that left keeps its last report, ready included
        assert self._status("ACTIVE", REPLACED, [True] * 4) == "ERROR"

    def test_node_set_that_is_not_active_speaks_for_the_instance(self) -> None:
        assert self._status("IN_PROGRESS", NODES, [True] * 4) == "IN_PROGRESS"

    def test_unknown_node_set_status_is_in_progress(self) -> None:
        assert self._status("SOMETHING", NODES, [True] * 4) == "IN_PROGRESS"

    def test_active_node_set_defers_to_rustfs(self) -> None:
        assert self._status("ACTIVE", NODES, [True] * 4) == "ACTIVE"
        assert self._status("ACTIVE", NODES, [True] * 3 + [False]) == "IN_PROGRESS"
        assert self._status("ACTIVE", NODES, None) is None


class TestBuildersAgree:
    """Both builders write one status, and neither undoes the other."""

    def _infra_builder(self) -> infra_builder.CoreInfraBuilder:
        builder = object.__new__(infra_builder.CoreInfraBuilder)
        builder._project_id = sys_uuid.uuid4()
        builder._cclient = mock.Mock()
        builder._cclient.do_action.return_value = {}
        return builder

    def _instance(self, status: str, members: dict) -> mock.Mock:
        instance = mock.Mock()
        instance.uuid = sys_uuid.uuid4()
        instance.name = "s3"
        instance.status = status
        instance.members = members
        instance.nodes_number = 4
        instance.parity = 1
        instance.root_secret = "secret"
        instance.cpu = 2
        instance.ram = 4096
        instance.disk_size = 10
        instance.version.image = "http://image"
        instance.is_distributed.return_value = True
        instance.get_buckets.return_value = []
        instance.get_policies.return_value = []
        instance.get_users.return_value = []
        return instance

    def _nodeset(self, status: str, nodes: dict) -> mock.Mock:
        nodeset = mock.Mock(status=status, nodes=nodes)
        nodeset.get_resource_kind.return_value = infra_builder.NODE_SET_KIND
        return nodeset

    def _statuses(self, start, members, nodeset_status, nodes, ready_flags):
        instance = self._instance(start, members)
        nodeset = self._nodeset(nodeset_status, nodes)
        instance.get_actual_nodeset.return_value = nodeset
        infra = sdk_infra_builder.InfraCollection(
            infra_objects=((self._nodeset(nodeset_status, {}), nodeset),)
        )
        seen = []
        with mock.patch.object(cluster, "readiness", return_value=ready_flags):
            for _ in range(2):
                self._infra_builder().actualize_infra(instance, infra)
                seen.append(instance.status)
                paas_builder.S3InstanceBuilder().actualize_paas_objects(
                    instance, sdk_paas_builder.PaaSCollection(paas_objects=())
                )
                seen.append(instance.status)
        return seen

    def test_waits_for_every_node_before_freezing(self) -> None:
        nodes = dict(list(NODES.items())[:3])

        assert set(self._statuses("NEW", {}, "IN_PROGRESS", nodes, None)) == {
            "IN_PROGRESS"
        }

    def test_drift_stays_an_error(self) -> None:
        statuses = self._statuses("ACTIVE", MEMBERS, "ACTIVE", REPLACED, [True] * 4)

        assert set(statuses) == {"ERROR"}

    def test_node_set_down_keeps_the_instance_down(self) -> None:
        statuses = self._statuses("ACTIVE", MEMBERS, "IN_PROGRESS", NODES, [True] * 4)

        assert set(statuses) == {"IN_PROGRESS"}

    def test_cluster_up_once_rustfs_serves(self) -> None:
        statuses = self._statuses("IN_PROGRESS", MEMBERS, "ACTIVE", NODES, [True] * 4)

        assert set(statuses) == {"ACTIVE"}

    def test_node_set_up_is_not_the_cluster_up(self) -> None:
        statuses = self._statuses(
            "IN_PROGRESS", MEMBERS, "ACTIVE", NODES, [True, True, True, False]
        )

        assert set(statuses) == {"IN_PROGRESS"}

    def test_silent_nodes_keep_the_verdict(self) -> None:
        statuses = self._statuses("ACTIVE", MEMBERS, "ACTIVE", NODES, None)

        assert set(statuses) == {"ACTIVE"}

    def test_infra_freezes_members_and_configures_every_node(self) -> None:
        instance = self._instance("NEW", {})
        nodeset = self._nodeset("ACTIVE", NODES)
        infra = sdk_infra_builder.InfraCollection(
            infra_objects=((self._nodeset("ACTIVE", {}), nodeset),)
        )

        with mock.patch.object(cluster, "readiness", return_value=None):
            objects = self._infra_builder().actualize_infra(instance, infra)

        assert instance.members == MEMBERS
        assert instance.ipsv4 == ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"]
        assert len(objects) == 5
