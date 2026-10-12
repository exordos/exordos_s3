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
from unittest import mock

from botocore.exceptions import ClientError
import pytest

from exordos_s3.tests.functional import conftest as helpers


def error(code):
    return ClientError({"Error": {"Code": code}}, "PutObject")


def test_upload_retries_transient_missing_bucket():
    client = mock.Mock()
    client.put_object.side_effect = [error("NoSuchBucket"), {}]
    with mock.patch.object(helpers.time, "sleep"):
        assert helpers.upload_test_object(client, "bucket", "key", "payload") == "key"
    assert client.put_object.call_args_list == [
        mock.call(Bucket="bucket", Key="key", Body=b"payload"),
        mock.call(Bucket="bucket", Key="key", Body=b"payload"),
    ]


@pytest.mark.parametrize("code", ["AccessDenied", "ServiceUnavailable"])
def test_upload_propagates_other_errors(code):
    client = mock.Mock()
    client.put_object.side_effect = error(code)
    with pytest.raises(ClientError) as exc:
        helpers.upload_test_object(client, "bucket", "key", b"payload")
    assert exc.value.response["Error"]["Code"] == code
    client.put_object.assert_called_once()


def test_upload_missing_bucket_retry_is_bounded():
    client = mock.Mock()
    client.put_object.side_effect = error("NoSuchBucket")
    with mock.patch.object(helpers.time, "monotonic", side_effect=[0, 1000]):
        with pytest.raises(ClientError) as exc:
            helpers.upload_test_object(client, "bucket", "key", b"payload")
    assert exc.value.response["Error"]["Code"] == "NoSuchBucket"
    client.put_object.assert_called_once()
