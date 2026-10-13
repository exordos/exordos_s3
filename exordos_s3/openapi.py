#    Copyright 2026 Genesis Corporation.
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

"""Build the S3 control-plane OpenAPI document without external services."""

import json
from unittest import mock

from restalchemy.api import applications
from restalchemy.api import constants as api_constants
from restalchemy.api import contexts as api_contexts
from restalchemy.api import controllers
from restalchemy.api import routes
from restalchemy.common import contexts
from restalchemy.openapi import engines
from restalchemy.openapi import structures
import webob

from exordos_s3.controlplane.api.routes import S3Route

OPENAPI_VERSION = "3.0.3"


class _TypesRoute(routes.Route):
    __controller__ = controllers.RoutesListController
    __allow_methods__ = [routes.FILTER]

    s3 = routes.route(S3Route)


class _VersionRoute(routes.Route):
    __controller__ = controllers.RoutesListController
    __allow_methods__ = [routes.FILTER]

    types = routes.route(_TypesRoute)


class _RootRoute(routes.RootRoute):
    v1 = routes.route(_VersionRoute)


def build(version: str = OPENAPI_VERSION) -> dict:
    """Describe the plugin at its runtime mount point, with all public fields."""
    context = contexts.ContextWithStorage()
    context.iam_context = mock.Mock()
    context.iam_context.introspection_info.return_value = {}
    context.iam_context.enforcer.enforce.return_value = True
    context.iam_context.enforcer.enforce_raw.return_value = True
    engine = engines.OpenApiEngine(
        info=structures.OpenApiInfo(
            title="Exordos S3 v1 User API",
            version="latest",
            description="S3 control-plane API hosted by Exordos MetaPaaS",
        ),
        paths=structures.OpenApiPaths(),
    )
    with context.context_manager():
        application = applications.OpenApiApplication(
            route_class=_RootRoute,
            openapi_engine=engine,
            warm_up_openapi=False,
        )
        request = webob.Request.blank("/")
        request.application = application
        request.api_context = api_contexts.RequestContext(request)
        request.api_context.set_active_method(api_constants.GET)
        specification = engine.build_openapi_specification(
            version=version, request=request
        )
    specification["paths"] = {
        path: methods
        for path, methods in specification["paths"].items()
        if path.startswith("/v1/types/s3/")
    }
    specification["servers"][0]["url"] = "http://127.0.0.1:8080"
    specification = json.loads(json.dumps(specification))
    # Credential factories generate a fresh value for each resource. Those
    # values are not API defaults and do not belong in a published document.
    for schema in specification["components"]["schemas"].values():
        for name, field in schema.get("properties", {}).items():
            if name in ("access_key", "secret_key", "root_secret"):
                field.pop("default", None)
    for parameter in specification["components"]["parameters"].values():
        if parameter.get("name") in ("access_key", "secret_key", "root_secret"):
            parameter.get("schema", {}).pop("default", None)
    return specification
