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
"""Distributed (multi-node RustFS) instance.

Four data plane VMs do not fit every runner, so the module runs only with
EXORDOS_S3_DISTRIBUTED=1.
"""

import logging
import os
import typing as tp
import uuid as sys_uuid

from bazooka import exceptions as bazooka_exc
import pytest

from exordos_s3 import constants
import exordos_s3.tests.functional.conftest as s3_conftest

LOG = logging.getLogger(__name__)

pytestmark = pytest.mark.skipif(
    os.environ.get("EXORDOS_S3_DISTRIBUTED") != "1",
    reason="set EXORDOS_S3_DISTRIBUTED=1 to run distributed instance tests",
)

NODES_NUMBER = 4
PARITY = 1
DISTRIBUTED_TIMEOUT = int(os.environ.get("EXORDOS_S3_DISTRIBUTED_TIMEOUT", "1800"))


@pytest.fixture(scope="module")
def distributed_instance(
    s3_api_client: tp.Any, s3_version_uuid: str, test_user_project: dict
) -> tp.Iterator[dict]:
    data = {
        "name": f"test-dist-{sys_uuid.uuid4().hex[:8]}",
        "project_id": test_user_project["uuid"],
        "kind": "distributed",
        "parity": PARITY,
        "cpu": 1,
        "ram": 1024,
        "disk_size": 10,
        "nodes_number": NODES_NUMBER,
        "version": f"{s3_conftest.S3_VERSIONS}{s3_version_uuid}",
    }
    instance = s3_api_client.create(s3_conftest.S3_INSTANCES, data=data)
    # A cluster that never turns ACTIVE is removed too: its nodes hold the
    # storage the next run needs
    try:
        yield s3_conftest._poll_instance_status(
            s3_api_client,
            instance["uuid"],
            "ACTIVE",
            DISTRIBUTED_TIMEOUT,
            s3_conftest.POLL_INTERVAL,
        )
    finally:
        if os.environ.get("EXORDOS_S3_KEEP_INSTANCES") == "1":
            # A cluster that misbehaves is only debuggable while its nodes live
            LOG.warning("Keeping S3 instance %s and its nodes", instance["uuid"])
        else:
            try:
                s3_api_client.delete(s3_conftest.S3_INSTANCES, uuid=instance["uuid"])
            except Exception:
                LOG.exception("Failed to clean up S3 instance %s", instance["uuid"])


@pytest.fixture(scope="module")
def node_endpoints(distributed_instance: dict) -> list[str]:
    return [f"{ip}:{constants.RUSTFS_PORT}" for ip in distributed_instance["ipsv4"]]


@pytest.fixture(scope="module")
def node_clients(
    s3_api_client: tp.Any,
    distributed_instance: dict,
    test_user_project: dict,
    node_endpoints: list[str],
) -> list:
    instance_uuid = distributed_instance["uuid"]
    project_id = test_user_project["uuid"]
    user = s3_conftest.create_user_via_api(
        s3_api_client, instance_uuid, f"dist-{sys_uuid.uuid4().hex[:8]}", project_id
    )
    policy = s3_conftest.create_policy_via_api(
        s3_api_client,
        instance_uuid,
        "dist-rw",
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:*"],
                    "Resource": ["arn:aws:s3:::*", "arn:aws:s3:::*/*"],
                }
            ],
        },
        project_id,
    )
    s3_conftest.attach_policy_via_api(
        s3_api_client, instance_uuid, user["uuid"], policy["uuid"], project_id
    )
    # The key is applied by one node; waiting on the last one proves it has
    # reached the whole cluster.
    key = s3_conftest.create_access_key_via_api(
        s3_api_client, instance_uuid, user["uuid"], project_id, node_endpoints[-1]
    )
    return [
        s3_conftest.make_s3_client(endpoint, key["access_key"], key["secret_key"])
        for endpoint in node_endpoints
    ]


class TestDistributedInstance:
    def test_topology(self, distributed_instance: dict) -> None:
        assert distributed_instance["kind"] == "distributed"
        assert distributed_instance["nodes_number"] == NODES_NUMBER
        assert distributed_instance["parity"] == PARITY
        assert len(distributed_instance["ipsv4"]) == NODES_NUMBER
        ordinals = sorted(
            m["ordinal"] for m in distributed_instance["members"].values()
        )
        assert ordinals == list(range(1, NODES_NUMBER + 1))

    def test_object_is_served_by_every_node(
        self,
        s3_api_client: tp.Any,
        distributed_instance: dict,
        test_user_project: dict,
        node_clients: list,
    ) -> None:
        bucket = f"dist-{sys_uuid.uuid4().hex[:8]}"
        s3_conftest.create_bucket_via_api(
            s3_api_client,
            distributed_instance["uuid"],
            bucket,
            test_user_project["uuid"],
            s3_client=node_clients[-1],
        )

        content = f"written-through-one-node-{sys_uuid.uuid4().hex}"
        s3_conftest.upload_test_object(node_clients[0], bucket, "object", content)

        for client in node_clients:
            assert (
                s3_conftest.download_object(client, bucket, "object").decode()
                == content
            )

    @pytest.mark.parametrize(
        ("field", "value", "code"),
        [
            # `parity` and `kind` are writable on create only, so the field
            # permissions refuse them before the model is even asked
            ("parity", PARITY + 1, 403),
            ("kind", "single_node", 403),
            ("nodes_number", NODES_NUMBER + 1, 400),
            ("cpu", 2, 400),
            ("ram", 2048, 400),
        ],
    )
    def test_layout_is_immutable(
        self,
        s3_api_client: tp.Any,
        distributed_instance: dict,
        field: str,
        value: tp.Any,
        code: int,
    ) -> None:
        with pytest.raises(bazooka_exc.BaseHTTPException) as exc_info:
            s3_api_client.update(
                s3_conftest.S3_INSTANCES,
                uuid=distributed_instance["uuid"],
                **{field: value},
            )

        # Any HTTP error would satisfy a bare `raises`, including a route that
        # stopped existing -- the refusal itself is what this asserts.
        response = exc_info.value.cause.response
        assert response.status_code == code
        if code == 400:
            assert f"{field} can't be changed" in response.text

    def test_disk_grows(
        self, s3_api_client: tp.Any, distributed_instance: dict
    ) -> None:
        disk_size = distributed_instance["disk_size"] + 5

        s3_api_client.update(
            s3_conftest.S3_INSTANCES,
            uuid=distributed_instance["uuid"],
            disk_size=disk_size,
        )

        instance = s3_api_client.get(
            s3_conftest.S3_INSTANCES, uuid=distributed_instance["uuid"]
        )
        assert instance["disk_size"] == disk_size
