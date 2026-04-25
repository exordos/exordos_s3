#!/usr/bin/env bash

# Copyright 2025 Genesis Corporation
#
# All Rights Reserved.
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
set -x
set -o pipefail


GC_PATH="/opt/genesis_s3"
GC_CFG_DIR=/etc/genesis_s3
VENV_PATH="$GC_PATH/.venv"
BOOTSTRAP_PATH="/var/lib/genesis/bootstrap/scripts"

SYSTEMD_SERVICE_DIR=/etc/systemd/system/

DEV_SDK_PATH="/opt/gcl_sdk"
SDK_DEV_MODE=$([ -d "$DEV_SDK_PATH" ] && echo "true" || echo "false")

# Install packages
sudo apt update
sudo apt dist-upgrade -y
sudo apt install -y \
    libev-dev \
    j2cli \
    postgresql-client-16

curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME"/.local/bin/env

# Install genesis s3
sudo mkdir -p $GC_CFG_DIR
sudo cp "$GC_PATH/etc/genesis_s3/genesis_s3.conf.j2" $GC_CFG_DIR/
sudo cp "$GC_PATH/etc/genesis_s3/core_agent.conf.j2" $GC_CFG_DIR/
sudo cp "$GC_PATH/etc/genesis_s3/logging.yaml" $GC_CFG_DIR/
sudo cp "$GC_PATH/genesis/images/cp_bootstrap.sh" $BOOTSTRAP_PATH/0100-gc-bootstrap.sh

cd "$GC_PATH"
uv sync
source "$GC_PATH"/.venv/bin/activate

# In the dev mode the gcl_sdk package is installed from the local machine
if [[ "$SDK_DEV_MODE" == "true" ]]; then
    uv pip uninstall -y gcl_sdk
    uv pip install -e "$DEV_SDK_PATH"
fi
deactivate

# Create links to venv
sudo ln -sf "$VENV_PATH/bin/genesis-s3-gservice" "/usr/bin/genesis-s3-gservice"
sudo ln -sf "$VENV_PATH/bin/genesis-s3-user-api" "/usr/bin/genesis-s3-user-api"
sudo ln -sf "$VENV_PATH/bin/genesis-s3-status-api" "/usr/bin/genesis-s3-status-api"
sudo ln -sf "$VENV_PATH/bin/genesis-s3-orch-api" "/usr/bin/genesis-s3-orch-api"
sudo ln -sf "$VENV_PATH/bin/genesis-universal-agent-db-back" "/usr/bin/genesis-universal-agent-db-back"

# Install Systemd service files
sudo cp "$GC_PATH/etc/systemd/genesis-s3-gservice.service" $SYSTEMD_SERVICE_DIR
sudo cp "$GC_PATH/etc/systemd/genesis-s3-user-api.service" $SYSTEMD_SERVICE_DIR
sudo cp "$GC_PATH/etc/systemd/genesis-s3-status-api.service" $SYSTEMD_SERVICE_DIR
sudo cp "$GC_PATH/etc/systemd/genesis-s3-orch-api.service" $SYSTEMD_SERVICE_DIR
sudo cp "$GC_PATH/etc/systemd/genesis-s3-core-agent.service" $SYSTEMD_SERVICE_DIR
