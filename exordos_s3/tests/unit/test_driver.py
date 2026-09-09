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
