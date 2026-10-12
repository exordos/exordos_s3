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

"""Write the OpenAPI document published with the S3 element."""

import argparse
from pathlib import Path

import ruamel.yaml

from exordos_s3 import openapi


def main() -> None:
    """Generate the user API YAML in the requested output directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output-dir", default="docs/openapi")
    parser.add_argument("--openapi-version", default=openapi.OPENAPI_VERSION)
    args = parser.parse_args()
    specification = openapi.build(version=args.openapi_version)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "openapi_user.yaml"
    dumper = ruamel.yaml.YAML()
    dumper.indent(sequence=4, offset=2)
    dumper.width = 4096
    dumper.representer.add_representer(
        type(None),
        lambda representer, value: representer.represent_scalar(
            "tag:yaml.org,2002:null", "null"
        ),
    )
    with path.open("w") as stream:
        dumper.dump(specification, stream)
    print(f"{path}: {len(specification['paths'])} paths")


if __name__ == "__main__":
    main()
