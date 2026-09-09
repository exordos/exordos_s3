#!/usr/bin/env bash

# Copyright 2026 Genesis Corporation
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

set -eu
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/../.."

# Don't rely on a system-wide `build` package — keep it inside a local venv
# so this script is self-contained and doesn't pollute the host. Prefer `uv`
# when available, fall back to plain `python3 -m venv` + `pip` otherwise.
VENV_PATH=".venv"
VENV_PY="$VENV_PATH/bin/python"

if command -v uv >/dev/null 2>&1; then
    [ -d "$VENV_PATH" ] || uv venv "$VENV_PATH"
    uv pip install --python "$VENV_PY" build
else
    [ -d "$VENV_PATH" ] || python3 -m venv "$VENV_PATH"
    "$VENV_PY" -m pip install --upgrade pip
    "$VENV_PY" -m pip install build
fi

# `python -m build` refuses to overwrite existing artifacts in dist/,
# so clean them up before rebuilding.
rm -rf dist/

"$VENV_PY" -m build --wheel