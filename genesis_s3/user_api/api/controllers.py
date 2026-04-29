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

from gcl_iam import controllers as iam_controllers
from restalchemy.api import constants
from restalchemy.api import controllers as ra_controllers
from restalchemy.api import field_permissions as field_p
from restalchemy.api import resources as ra_resources

from genesis_s3.user_api.api import versions
from genesis_s3.user_api.dm import models


class ApiEndpointController(ra_controllers.RoutesListController):
    """Controller for /v1/ endpoint"""

    __TARGET_PATH__ = f"/{versions.API_VERSION_1_0}/"


class TypeController(ra_controllers.Controller):
    def filter(self, filters, order_by):
        return ["s3"]


class S3Controller(ra_controllers.RoutesListController):
    """Controller for /v1/types/s3/ endpoint"""

    __TARGET_PATH__ = f"/{versions.API_VERSION_1_0}/types/s3/"


class S3VersionController(
    iam_controllers.PolicyBasedWithoutProjectController,
    ra_controllers.BaseResourceControllerPaginated,
):
    __policy_service_name__ = "genesis_s3"
    __policy_name__ = "s3_version"

    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.S3Version,
        convert_underscore=False,
        process_filters=True,
    )


class S3InstanceController(
    iam_controllers.PolicyBasedController,
    ra_controllers.BaseResourceControllerPaginated,
):
    __policy_service_name__ = "genesis_s3"
    __policy_name__ = "s3_instance"

    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.S3Instance,
        convert_underscore=False,
        process_filters=True,
        fields_permissions=field_p.FieldsPermissions(
            default=field_p.Permissions.RW,
            fields={
                "status": {constants.ALL: field_p.Permissions.RO},
                "ipsv4": {constants.ALL: field_p.Permissions.RO},
                "kind": {constants.ALL: field_p.Permissions.RO},
                "root_secret": {constants.ALL: field_p.Permissions.HIDDEN},
            },
        ),
    )


class S3BucketController(
    iam_controllers.NestedPolicyBasedController,
    ra_controllers.BaseNestedResourceControllerPaginated,
):
    __policy_service_name__ = "genesis_s3"
    __policy_name__ = "bucket"
    __pr_name__ = "instance"

    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.S3Bucket,
        convert_underscore=False,
        process_filters=True,
        fields_permissions=field_p.FieldsPermissions(
            default=field_p.Permissions.RW,
            fields={
                "status": {constants.ALL: field_p.Permissions.RO},
                "name": {
                    constants.ALL: field_p.Permissions.RO,
                    constants.CREATE: field_p.Permissions.RW,
                },
                "versioning_enabled": {
                    constants.ALL: field_p.Permissions.RO,
                    constants.CREATE: field_p.Permissions.RW,
                },
                "object_lock_enabled": {
                    constants.ALL: field_p.Permissions.RO,
                    constants.CREATE: field_p.Permissions.RW,
                },
            },
        ),
    )


class S3PolicyController(
    iam_controllers.NestedPolicyBasedController,
    ra_controllers.BaseNestedResourceControllerPaginated,
):
    __policy_service_name__ = "genesis_s3"
    __policy_name__ = "policy"
    __pr_name__ = "instance"

    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.S3Policy,
        convert_underscore=False,
        process_filters=True,
        fields_permissions=field_p.FieldsPermissions(
            default=field_p.Permissions.RW,
            fields={
                "status": {constants.ALL: field_p.Permissions.RO},
            },
        ),
    )


class S3UserController(
    iam_controllers.NestedPolicyBasedController,
    ra_controllers.BaseNestedResourceControllerPaginated,
):
    __policy_service_name__ = "genesis_s3"
    __policy_name__ = "user"
    __pr_name__ = "instance"

    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.S3User,
        convert_underscore=False,
        process_filters=True,
        fields_permissions=field_p.FieldsPermissions(
            default=field_p.Permissions.RW,
            fields={
                "status": {constants.ALL: field_p.Permissions.RO},
            },
        ),
    )


class S3AccessKeyController(
    iam_controllers.NestedPolicyBasedController,
    ra_controllers.BaseNestedResourceControllerPaginated,
):
    __policy_service_name__ = "genesis_s3"
    __policy_name__ = "access_key"
    __pr_name__ = "user"

    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.S3AccessKey,
        convert_underscore=False,
        process_filters=True,
        fields_permissions=field_p.FieldsPermissions(
            default=field_p.Permissions.RW,
            fields={
                "access_key": {constants.ALL: field_p.Permissions.RO},
                "secret_key": {
                    constants.ALL: field_p.Permissions.HIDDEN,
                    constants.CREATE: field_p.Permissions.RW,
                },
                "status": {constants.ALL: field_p.Permissions.RO},
            },
        ),
    )


class S3UserPolicyAttachmentController(
    iam_controllers.NestedPolicyBasedController,
    ra_controllers.BaseNestedResourceControllerPaginated,
):
    __policy_service_name__ = "genesis_s3"
    __policy_name__ = "policy"
    __pr_name__ = "user"

    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.S3UserPolicyAttachment,
        convert_underscore=False,
        process_filters=True,
        fields_permissions=field_p.FieldsPermissions(
            default=field_p.Permissions.RW,
            fields={
                "uuid": {constants.ALL: field_p.Permissions.RO},
                "created_at": {constants.ALL: field_p.Permissions.RO},
                "updated_at": {constants.ALL: field_p.Permissions.RO},
                "status": {constants.ALL: field_p.Permissions.RO},
            },
        ),
    )
