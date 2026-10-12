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

"""Opt-in replication test against two isolated RustFS processes.

Run with EXORDOS_TEST_RUSTFS_BINARY pointing to the RustFS 1.0.1 executable.
"""

import io
import os
from pathlib import Path
import socket
import subprocess
import time
from unittest import mock
import uuid

from minio import Minio
from minio.error import S3Error
from minio.versioningconfig import ENABLED
from minio.versioningconfig import SUSPENDED
from minio.versioningconfig import VersioningConfig
from jinja2 import Template
import pytest
import requests
import yaml

from exordos_s3.dataplane import driver
from exordos_s3.dataplane import replication


def wait_until(predicate, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (requests.RequestException, S3Error):
            pass
        time.sleep(0.2)
    raise AssertionError("RustFS did not converge before the deadline")


@pytest.fixture
def metrics_store(tmp_path):
    binary = os.environ.get("EXORDOS_TEST_VICTORIAMETRICS_BINARY")
    if not binary:
        yield None
        return
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    with (tmp_path / "victoriametrics.log").open("wb") as log:
        process = subprocess.Popen(
            [
                str(Path(binary).resolve()),
                f"-httpListenAddr=127.0.0.1:{port}",
                f"-storageDataPath={tmp_path / 'metrics-data'}",
                "-memory.allowedBytes=268435456",
                "-search.latencyOffset=0s",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        wait_until(
            lambda: requests.get(endpoint + "/health", timeout=1).status_code == 200
        )
        yield endpoint
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@pytest.fixture
def rustfs_pair(tmp_path, metrics_store):
    binary = os.environ.get("EXORDOS_TEST_RUSTFS_BINARY")
    if not binary:
        pytest.skip("set EXORDOS_TEST_RUSTFS_BINARY to run isolated RustFS replication")
    processes = []
    clients = []
    try:
        for ordinal in range(2):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            directory = tmp_path / str(ordinal)
            directory.mkdir()
            (directory / "data").mkdir()
            env = os.environ | {
                "RUSTFS_ACCESS_KEY": "replication-test",
                "RUSTFS_SECRET_KEY": "isolated-replication-test-secret",
                "RUSTFS_CONSOLE_ENABLE": "false",
                "RUSTFS_OBS_LOG_DIRECTORY": str(directory / "logs"),
                "RUSTFS_REGION": "us-east-1",
                "RUSTFS_REPLICATION_ALLOW_LOOPBACK_TARGET": "true",
                "RUSTFS_SCANNER_CYCLE": "1",
                "RUSTFS_SCANNER_START_DELAY_SECS": "1",
                "RUSTFS_DATA_USAGE_UPDATE_DIR_CYCLES": "1",
            }
            if metrics_store:
                env |= {
                    "RUSTFS_OBS_ENDPOINT": metrics_store + "/opentelemetry",
                    "RUSTFS_OBS_METRIC_ENDPOINT": metrics_store
                    + "/opentelemetry/v1/metrics",
                    "RUSTFS_OBS_METRICS_EXPORT_ENABLED": "true",
                    "RUSTFS_OBS_TRACES_EXPORT_ENABLED": "false",
                    "RUSTFS_OBS_LOGS_EXPORT_ENABLED": "false",
                    "RUSTFS_OBS_USE_STDOUT": "false",
                    "RUSTFS_OBS_METER_INTERVAL": "1",
                    "RUSTFS_METRICS_BUCKET_REPLICATION_BANDWIDTH_INTERVAL_SEC": "1",
                    "OTEL_RESOURCE_ATTRIBUTES": (
                        f"exordos_s3_instance=replication-test-{ordinal},"
                        f"exordos_project=replication-test,rustfs.node=node-{ordinal}"
                    ),
                }
            with (directory / "server.log").open("wb") as log:
                process = subprocess.Popen(
                    [
                        str(Path(binary).resolve()),
                        "server",
                        "--address",
                        f"127.0.0.1:{port}",
                        str(directory / "data"),
                    ],
                    env=env,
                    cwd=directory,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            processes.append(process)
            wait_until(
                lambda: (
                    requests.get(
                        f"http://127.0.0.1:{port}/health/ready", timeout=1
                    ).status_code
                    == 200
                )
            )
            client = driver.AdminClient.__new__(driver.AdminClient)
            client._endpoint = f"127.0.0.1:{port}"
            client._root_user = env["RUSTFS_ACCESS_KEY"]
            client._root_password = env["RUSTFS_SECRET_KEY"]
            client._metrics_endpoint = metrics_store
            client._admin_base_url = f"http://{client._endpoint}/rustfs/admin/v3"
            client._client = Minio(
                client._endpoint,
                access_key=client._root_user,
                secret_key=client._root_password,
                secure=False,
                region="us-east-1",
            )
            clients.append(client)
        yield clients
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def assert_replication_dashboard_metrics(source, arn):
    endpoint = source._metrics_endpoint
    selector = (
        '{exordos_s3_instance="replication-test-0",bucket="source-bucket",target_arn="'
        + arn
        + '"}'
    )

    def query(expression):
        response = requests.get(
            endpoint + "/api/v1/query", params={"query": expression}, timeout=5
        )
        response.raise_for_status()
        payload = response.json()
        assert payload["status"] == "success", payload
        return payload["data"]["result"]

    wait_until(
        lambda: any(
            float(point["value"][1]) > 0
            for point in query("rustfs_bucket_replication_target_sent_count" + selector)
        )
    )
    manifest = Path(__file__).parents[3] / "exordos/manifests/s3_dashboard.yaml.j2"
    document = yaml.safe_load(Template(manifest.read_text()).render(version="test"))
    dashboard = document["resources"]["$grafanaaas.types.grafana.dashboards"]["s3"][
        "source"
    ]["content"]
    in_replication = False
    for panel in dashboard["panels"]:
        if panel["type"] == "row":
            in_replication = panel["title"] == "Replication"
        elif in_replication and panel["type"] != "logs":
            for target in panel["targets"]:
                expression = (
                    target["expr"]
                    .replace("$s3", "replication-test-0")
                    .replace("$__range", "5m")
                )
                try:
                    wait_until(lambda: bool(query(expression)))
                except AssertionError as exc:
                    raise AssertionError(
                        f"Dashboard query has no data: {expression}"
                    ) from exc


@pytest.mark.parametrize(
    "replicate_delete_markers",
    [True, False],
    ids=["markers-enabled", "markers-disabled"],
)
def test_replication_filters_updates_and_decommission(
    rustfs_pair, tmp_path, replicate_delete_markers
):
    source, destination = rustfs_pair
    source.client.make_bucket("source-bucket")
    destination.client.make_bucket("backup-bucket")
    for client, bucket in ((source, "source-bucket"), (destination, "backup-bucket")):
        client.client.set_bucket_versioning(bucket, VersioningConfig(ENABLED))
    destination.add_user("backup-writer", "initial-backup-writer-secret")
    destination.set_user_policies("backup-writer", ["readwrite"])
    source.client.put_object(
        "source-bucket", "keep/existing", io.BytesIO(b"existing"), 8
    )
    source.client.put_object(
        "source-bucket", "skip/existing", io.BytesIO(b"excluded"), 8
    )
    target = dict(
        endpoint=f"http://{destination._endpoint}",
        destination_bucket="backup-bucket",
        region="us-east-1",
        access_key="backup-writer",
        ca_cert_pem="",
        bucket_lookup="path",
        storage_class="STANDARD",
        bandwidth_limit=0,
        synchronous=False,
        disable_proxy=True,
        credential_revision="test-revision",
        rules={
            "test-rule": dict(
                enabled=True,
                priority=10,
                prefix="keep/",
                tags={},
                replicate_existing=True,
                replicate_delete_markers=replicate_delete_markers,
                replicate_deletes=False,
            )
        },
    )
    desired = {"source-bucket": {"test-target": target}}
    credentials = {"test-target": {"secret_key": "initial-backup-writer-secret"}}
    manager = replication.ReplicationManager(source, str(tmp_path / "journal.json"))
    assert manager.apply(desired, credentials, {"source-bucket"})
    assert manager.matches(desired)
    # Reused agent payloads omit credentials already applied to the remote target.
    assert manager.apply(desired, {}, {"source-bucket"})
    assert manager.matches(desired)
    arn = manager.bindings["test-target"]["arn"]
    wait_until(
        lambda: (
            destination.client.stat_object("backup-bucket", "keep/existing").size == 8
        )
    )
    with pytest.raises(S3Error, match="NoSuchKey"):
        destination.client.stat_object("backup-bucket", "skip/existing")
    version = source.client.put_object(
        "source-bucket", "keep/object", io.BytesIO(b"replicated"), 10
    ).version_id
    source.client.put_object("source-bucket", "skip/object", io.BytesIO(b"excluded"), 8)
    wait_until(
        lambda: (
            destination.client.stat_object("backup-bucket", "keep/object").size == 10
        )
    )
    with pytest.raises(S3Error, match="NoSuchKey"):
        destination.client.stat_object("backup-bucket", "skip/object")
    source.client.remove_object("source-bucket", "keep/object")
    if replicate_delete_markers:
        wait_until(
            lambda: any(
                entry.object_name == "keep/object" and entry.is_delete_marker
                for entry in destination.client.list_objects(
                    "backup-bucket", prefix="keep/object", include_version=True
                )
            )
        )
        with pytest.raises(S3Error, match="NoSuchKey"):
            destination.client.stat_object("backup-bucket", "keep/object")
    else:
        assert destination.client.stat_object("backup-bucket", "keep/object").size == 10
    source.client.remove_object("source-bucket", "keep/object", version_id=version)
    with pytest.raises(S3Error, match="NoSuchKey|NoSuchVersion"):
        source.client.stat_object("source-bucket", "keep/object", version_id=version)
    # The receiver rejects the old secret before source credentials are rotated.
    destination.add_user("backup-writer", "rotated-backup-writer-secret")
    credentials["test-target"]["secret_key"] = "rotated-backup-writer-secret"
    target["credential_revision"] = "secret-rotation"
    assert manager.apply(desired, credentials, {"source-bucket"})
    assert manager.matches(desired)
    source.client.put_object(
        "source-bucket", "keep/rotated-secret", io.BytesIO(b"rotated"), 7
    )
    wait_until(
        lambda: (
            destination.client.stat_object("backup-bucket", "keep/rotated-secret").size
            == 7
        )
    )
    destination.add_user("another-backup-writer", "another-backup-writer-secret")
    destination.set_user_policies("another-backup-writer", ["readwrite"])
    target["access_key"] = "another-backup-writer"
    target["credential_revision"] = "access-key-rotation"
    credentials["test-target"]["secret_key"] = "another-backup-writer-secret"
    assert manager.apply(desired, credentials, {"source-bucket"})
    assert manager.matches(desired)
    source.client.put_object(
        "source-bucket", "keep/rotated-access", io.BytesIO(b"rotated"), 7
    )
    wait_until(
        lambda: (
            destination.client.stat_object("backup-bucket", "keep/rotated-access").size
            == 7
        )
    )
    target.update(bandwidth_limit=1048576, synchronous=True)
    assert manager.apply(desired, credentials, {"source-bucket"})
    assert manager.matches(desired)
    assert manager.bindings["test-target"]["arn"] == arn
    if source._metrics_endpoint:
        assert_replication_dashboard_metrics(source, arn)
    target["rules"]["test-rule"]["enabled"] = False
    assert manager.apply(desired, credentials, {"source-bucket"})
    assert manager.matches(desired)
    source.client.put_object("source-bucket", "keep/paused", io.BytesIO(b"paused"), 6)
    with pytest.raises(S3Error, match="NoSuchKey"):
        destination.client.stat_object("backup-bucket", "keep/paused")
    with mock.patch.object(driver, "AdminClient"):
        node = driver.S3Instance(uuid=uuid.uuid4(), name="node")
    node.mc = source
    node.buckets = {
        "source-bucket": source.get_bucket_state("source-bucket")
        | {"versioning_enabled": False, "quota_bytes": 0}
    }
    with (
        mock.patch.object(node, "_replication_manager", return_value=manager),
        mock.patch.object(
            node, "_fill_ready", side_effect=lambda: setattr(node, "ready", True)
        ),
    ):
        node.dump_to_dp()
    assert source.client.get_bucket_versioning("source-bucket").status == SUSPENDED
    assert manager.matches({})
    assert not manager._targets("source-bucket")
    response = destination.client.get_object(
        "backup-bucket", "keep/object", version_id=version
    )
    try:
        assert response.read() == b"replicated"
    finally:
        response.close()
        response.release_conn()
