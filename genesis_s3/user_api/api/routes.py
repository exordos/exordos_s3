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

from restalchemy.api import routes

from genesis_s3.user_api.api import controllers


class S3AccessKeyRoute(routes.Route):
    __controller__ = controllers.S3AccessKeyController


class S3UserPolicyAttachmentRoute(routes.Route):
    __controller__ = controllers.S3UserPolicyAttachmentController


class S3UserRoute(routes.Route):
    __controller__ = controllers.S3UserController

    # route to /v1/types/s3/instances/<uuid>/users/<uuid>/keys/[<uuid>]
    keys = routes.route(S3AccessKeyRoute, resource_route=True)
    # route to /v1/types/s3/instances/<uuid>/users/<uuid>/policies/[<uuid>]
    policies = routes.route(S3UserPolicyAttachmentRoute, resource_route=True)


class S3BucketRoute(routes.Route):
    __controller__ = controllers.S3BucketController


class S3PolicyRoute(routes.Route):
    __controller__ = controllers.S3PolicyController


class S3InstanceRoute(routes.Route):
    __controller__ = controllers.S3InstanceController

    # route to /v1/types/s3/instances/<uuid>/buckets/[<uuid>]
    buckets = routes.route(S3BucketRoute, resource_route=True)
    # route to /v1/types/s3/instances/<uuid>/policies/[<uuid>]
    policies = routes.route(S3PolicyRoute, resource_route=True)
    # route to /v1/types/s3/instances/<uuid>/users/[<uuid>]
    users = routes.route(S3UserRoute, resource_route=True)


class S3VersionRoute(routes.Route):
    __controller__ = controllers.S3VersionController


class S3Route(routes.Route):
    __controller__ = controllers.S3Controller
    __allow_methods__ = [routes.FILTER]

    # route to /v1/types/s3/instances/[<uuid>]
    instances = routes.route(S3InstanceRoute)

    # route to /v1/types/s3/versions/[<uuid>]
    versions = routes.route(S3VersionRoute)


class TypeRoute(routes.Route):
    """Handler for /v1/types/ endpoint"""

    __controller__ = controllers.TypeController
    __allow_methods__ = [routes.FILTER]

    # route to /v1/types/s3/
    s3 = routes.route(S3Route)


class ApiEndpointRoute(routes.Route):
    """Handler for /v1/ endpoint"""

    __controller__ = controllers.ApiEndpointController
    __allow_methods__ = [routes.FILTER]

    types = routes.route(TypeRoute)
