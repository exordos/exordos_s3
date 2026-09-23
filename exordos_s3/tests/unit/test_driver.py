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

import json
import logging
import typing as tp
import uuid
from unittest import mock

import pytest
import requests

from exordos_s3.dataplane import driver


def _admin_client() -> driver.AdminClient:
    """Build an AdminClient without touching the local rustfs env file."""
    return driver.AdminClient.__new__(driver.AdminClient)


def _http_error(status_code: int, text: str) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    response._content = text.encode("utf-8")
    return requests.HTTPError(f"{status_code} Server Error", response=response)


SYSTEM_POLICY_IN_BODY = _http_error(
    500,
    "<Error><Code>InternalError</Code><Message>io error: "
    f"{driver.SYSTEM_POLICY_DELETE_ERROR}</Message></Error>",
)

# The same refusal as RustFS could word it: carried by the status line, with
# nothing in the body to go on.
SYSTEM_POLICY_IN_MESSAGE = requests.HTTPError(
    f"500 Server Error: io error: {driver.SYSTEM_POLICY_DELETE_ERROR} for url: /x"
)


class TestRemovePolicy:
    def test_removes_policy(self) -> None:
        with mock.patch.object(driver.AdminClient, "_admin_request") as request:
            _admin_client().remove_policy("my policy")

        request.assert_called_once_with(
            "DELETE", "/remove-canned-policy?name=my%20policy"
        )

    @pytest.mark.parametrize("error", [SYSTEM_POLICY_IN_BODY, SYSTEM_POLICY_IN_MESSAGE])
    def test_system_policy_refusal_is_not_a_warning(
        self, caplog: pytest.LogCaptureFixture, error: requests.HTTPError
    ) -> None:
        with (
            caplog.at_level(logging.DEBUG, logger=driver.LOG.name),
            mock.patch.object(driver.AdminClient, "_admin_request", side_effect=error),
        ):
            _admin_client().remove_policy("KMSAuditor")

        assert [r.levelno for r in caplog.records] == [logging.DEBUG]

    def test_other_errors_are_warned_about(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            caplog.at_level(logging.DEBUG, logger=driver.LOG.name),
            mock.patch.object(
                driver.AdminClient,
                "_admin_request",
                side_effect=_http_error(500, "boom"),
            ),
        ):
            _admin_client().remove_policy("my-policy")

        assert [r.levelno for r in caplog.records] == [logging.WARNING]


@pytest.mark.parametrize("name", ["KMSAuditor", "KMSKeyAdministrator", "KMSKeyUser"])
def test_kms_policies_are_system_policies(name: str) -> None:
    assert name in driver.SYSTEM_POLICIES


class TestBucketQuota:
    def _quota(self, response: tp.Any) -> int | None:
        client = _admin_client()
        with mock.patch.object(client, "_admin_request", side_effect=[response]):
            return client.get_bucket_quota("b")

    def _response(self, body: dict) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(body).encode("utf-8")
        return response

    def test_reads_the_quota(self) -> None:
        assert self._quota(self._response({"bucket": "b", "quota": 1024})) == 1024

    def test_reads_the_quota_without_usage(self) -> None:
        # /quota/<bucket> waits for the scanner to count the bucket's usage
        client = _admin_client()
        with mock.patch.object(
            client,
            "_admin_request",
            return_value=self._response({"quota": 1024, "size": 1024}),
        ) as request:
            client.get_bucket_quota("b")

        request.assert_called_once_with("GET", "/get-bucket-quota?bucket=b")

    def test_no_quota_is_zero(self) -> None:
        assert self._quota(self._response({"bucket": "b", "quota": None})) == 0

    def test_unanswered_quota_is_unknown(self) -> None:
        # RustFS 1.0 until it has counted the usage of the bucket
        unavailable = _http_error(
            503,
            "<Error><Code>ServiceUnavailable</Code><Message>authoritative "
            "bucket usage is not available yet</Message></Error>",
        )

        assert self._quota(unavailable) is None


class TestQuotaReconciliation:
    def _instance(self, quota: int | None) -> driver.S3Instance:
        with mock.patch.object(driver, "AdminClient"):
            instance = driver.S3Instance(
                uuid=uuid.uuid4(),
                name="s3",
                buckets={"b": {"quota_bytes": 1024}},
            )
        instance.mc.get_bucket_state.return_value = {}
        instance.mc.get_bucket_quota.return_value = quota
        return instance

    def test_unknown_quota_is_left_alone(self) -> None:
        instance = self._instance(None)

        instance._reconcile_buckets({"b": {}})

        instance.mc.set_bucket_quota.assert_not_called()
        instance.mc.clear_bucket_quota.assert_not_called()

    def test_differing_quota_is_set(self) -> None:
        instance = self._instance(0)

        instance._reconcile_buckets({"b": {}})

        instance.mc.set_bucket_quota.assert_called_once_with("b", 1024)

    def test_matching_quota_is_not_set_again(self) -> None:
        instance = self._instance(1024)

        instance._reconcile_buckets({"b": {}})

        instance.mc.set_bucket_quota.assert_not_called()

    def test_refused_quota_does_not_stop_the_pass(self) -> None:
        # RustFS 1.0 answers 503 to setting a quota right after start
        instance = self._instance(0)
        instance.mc.set_bucket_quota.side_effect = _http_error(503, "boom")

        instance._reconcile_buckets({"b": {}, "gone": {}})

        instance.mc.remove_bucket.assert_called_once_with("gone")


class TestReconciler:
    def _instance(self, **kwargs: tp.Any) -> driver.S3Instance:
        with mock.patch.object(driver, "AdminClient"):
            return driver.S3Instance(name="s3", **kwargs)

    def test_follower_does_not_touch_rustfs(self) -> None:
        instance = self._instance(uuid=uuid.uuid4(), reconciler=False)

        instance.dump_to_dp()

        instance.mc.list_policies.assert_not_called()
        instance.mc.list_users.assert_not_called()
        instance.mc.list_buckets.assert_not_called()

    def test_reconciler_applies_the_target(self) -> None:
        instance = self._instance(uuid=uuid.uuid4())
        instance.mc.list_policies.return_value = {}
        instance.mc.list_users.return_value = {}
        instance.mc.list_buckets.return_value = {}

        with mock.patch.object(
            instance, "_fill_ready", side_effect=lambda: setattr(instance, "ready", True)
        ):
            instance.dump_to_dp()

        instance.mc.list_buckets.assert_called_once_with()

    def test_flag_is_kept_in_the_meta_file(self) -> None:
        instance = self._instance(uuid=uuid.uuid4(), reconciler=False)

        assert "reconciler" in instance.get_meta_fields()


class TestReadiness:
    def _instance(self, **kwargs: tp.Any) -> driver.S3Instance:
        with mock.patch.object(driver, "AdminClient"):
            return driver.S3Instance(uuid=uuid.uuid4(), name="s3", **kwargs)

    @pytest.mark.parametrize(("status_code", "expected"), [(200, True), (503, False)])
    def test_reads_the_local_readiness_probe(
        self, status_code: int, expected: bool
    ) -> None:
        instance = self._instance()
        response = requests.Response()
        response.status_code = status_code

        with mock.patch.object(driver.requests, "get", return_value=response) as get:
            instance._fill_ready()

        get.assert_called_once_with(driver.constants.RUSTFS_READY_URL, timeout=5)
        assert instance.ready is expected

    def test_a_node_that_does_not_answer_is_not_ready(self) -> None:
        instance = self._instance(ready=True)

        with mock.patch.object(
            driver.requests, "get", side_effect=requests.ConnectionError("refused")
        ):
            instance._fill_ready()

        assert instance.ready is False


class TestReadinessOnApply:
    def _instance(self, **kwargs: tp.Any) -> driver.S3Instance:
        with mock.patch.object(driver, "AdminClient"):
            return driver.S3Instance(uuid=uuid.uuid4(), name="s3", **kwargs)

    def _ready_response(self) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        return response

    def test_applying_the_target_also_reports_readiness(self) -> None:
        # The agent reports the model it applied: a field only the node can
        # answer is stale unless this path fills it too
        instance = self._instance()
        instance.mc.list_policies.return_value = {}
        instance.mc.list_users.return_value = {}
        instance.mc.list_buckets.return_value = {}

        with mock.patch.object(
            driver.requests, "get", return_value=self._ready_response()
        ):
            instance.dump_to_dp()

        assert instance.ready is True

    def test_a_follower_reports_readiness_too(self) -> None:
        instance = self._instance(reconciler=False)

        with mock.patch.object(
            driver.requests, "get", return_value=self._ready_response()
        ):
            instance.dump_to_dp()

        assert instance.ready is True
        instance.mc.list_buckets.assert_not_called()

    def test_an_unready_reconciler_reports_rather_than_fails(self) -> None:
        # A failed create drops the node's report on the control plane, and
        # the instance keeps whatever status it had -- ACTIVE after a reinstall
        instance = self._instance()
        instance.mc.list_policies.side_effect = RuntimeError("503")
        response = requests.Response()
        response.status_code = 503

        with mock.patch.object(driver.requests, "get", return_value=response):
            instance.dump_to_dp()

        assert instance.ready is False
        instance.mc.list_policies.assert_not_called()

    def test_an_unready_node_reports_rather_than_fails_on_read(self) -> None:
        # The read runs every iteration: a 503 from the admin API there drops
        # the node's report just as a failed create does
        instance = self._instance()
        instance.mc.list_policies.side_effect = RuntimeError("503")
        response = requests.Response()
        response.status_code = 503

        with mock.patch.object(driver.requests, "get", return_value=response):
            instance.restore_from_dp()

        assert instance.ready is False
        instance.mc.list_policies.assert_not_called()


class TestScannerRecovery:
    @pytest.fixture(autouse=True)
    def _fresh_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(driver, "_scanner_checked_at", None)

    def _instance(self, state: str | None) -> driver.S3Instance:
        with mock.patch.object(driver, "AdminClient"):
            instance = driver.S3Instance(uuid=uuid.uuid4(), name="s3")
        instance.mc.scanner_state.return_value = state
        return instance

    def test_a_scanner_stopped_on_the_usage_floor_is_rebuilt(self) -> None:
        instance = self._instance(driver.SCANNER_USAGE_FLOOR_FAILED)

        instance._recover_scanner()

        instance.mc.rebuild_scanner_usage.assert_called_once_with()

    def test_a_running_scanner_is_left_alone(self) -> None:
        instance = self._instance("held")

        instance._recover_scanner()

        instance.mc.rebuild_scanner_usage.assert_not_called()

    def test_the_scanner_is_checked_once_per_interval(self) -> None:
        instance = self._instance("held")

        instance._recover_scanner()
        instance._recover_scanner()

        instance.mc.scanner_state.assert_called_once_with()

    def test_a_failed_check_does_not_fail_the_read(self) -> None:
        instance = self._instance(None)
        instance.mc.scanner_state.side_effect = requests.ConnectionError()

        instance._recover_scanner()

        instance.mc.rebuild_scanner_usage.assert_not_called()

    def test_an_unready_node_does_not_touch_the_scanner(self) -> None:
        instance = self._instance(driver.SCANNER_USAGE_FLOOR_FAILED)
        response = requests.Response()
        response.status_code = 503

        with (
            mock.patch.object(driver.requests, "get", return_value=response),
            mock.patch.object(instance, "_fill_actual_policies"),
            mock.patch.object(instance, "_fill_actual_users"),
            mock.patch.object(instance, "_fill_actual_buckets"),
        ):
            instance.restore_from_dp()

        instance.mc.scanner_state.assert_not_called()
