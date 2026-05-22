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


GC_PATH="/opt/exordos_s3"
GC_CFG_DIR=/etc/exordos_s3
RUSTFS_CFG_DIR=/etc/exordos_s3
WORK_DIR="/var/lib/exordos/exordos_s3"
RUSTFS_DATA_DIR="/var/lib/rustfs/data"
VENV_PATH="$GC_PATH/.venv"
BOOTSTRAP_PATH="/var/lib/exordos/bootstrap/scripts"

SYSTEMD_SERVICE_DIR=/etc/systemd/system/

DEV_SDK_PATH="/opt/gcl_sdk"
SDK_DEV_MODE=$([ -d "$DEV_SDK_PATH" ] && echo "true" || echo "false")

# Install packages
sudo apt update
sudo apt dist-upgrade -y
sudo apt install -y \
    libev-dev unzip

# Install rustfs binary
ARCH=$(uname -m)
case "$ARCH" in
    x86_64)
        # temporary download from exordos repo due to download error from github
        RUSTFS_PKG_URL="https://repo.exordos.com/rustfs/rustfs-linux-x86_64-gnu-latest.zip"
        # RUSTFS_PKG_URL="https://dl.rustfs.com/artifacts/rustfs/release/rustfs-linux-x86_64-gnu-latest.zip"
        ;;
    aarch64)
        RUSTFS_PKG_URL="https://dl.rustfs.com/artifacts/rustfs/release/rustfs-linux-aarch64-gnu-latest.zip"
        ;;
    *)
        echo "Unsupported CPU architecture: $ARCH" >&2
        exit 1
        ;;
esac

TMP_DIR=$(mktemp -d)
curl -L -o "$TMP_DIR/rustfs.zip" "$RUSTFS_PKG_URL"
unzip "$TMP_DIR/rustfs.zip" -d "$TMP_DIR"
RUSTFS_BIN=$(find "$TMP_DIR" -type f -name rustfs | head -n1)
sudo cp "$RUSTFS_BIN" /usr/bin/rustfs
sudo chmod +x /usr/bin/rustfs
rm -rf "$TMP_DIR"

# Create directories
sudo mkdir -p $GC_CFG_DIR
sudo mkdir -p $RUSTFS_CFG_DIR
sudo mkdir -p $WORK_DIR
sudo mkdir -p $RUSTFS_DATA_DIR

# Install exordos s3 agent config and bootstrap
sudo cp "$GC_PATH/etc/exordos_s3/exordos_s3_agent.conf" $GC_CFG_DIR/
sudo cp "$GC_PATH/etc/exordos_s3/logging.yaml" $GC_CFG_DIR/
sudo cp "$GC_PATH/exordos/images/dp_bootstrap.sh" $BOOTSTRAP_PATH/0100-s3-bootstrap.sh

cd "$GC_PATH"
uv sync
source "$GC_PATH/.venv/bin/activate"

# In the dev mode the gcl_sdk package is installed from the local machine
if [[ "$SDK_DEV_MODE" == "true" ]]; then
    uv pip uninstall -y gcl_sdk
    uv pip install -e "$DEV_SDK_PATH"
fi

# Create links to venv
sudo ln -sf "$VENV_PATH/bin/exordos-universal-agent" "/usr/bin/exordos-s3-agent"

deactivate

# Install Systemd service files
sudo cp "$GC_PATH/etc/systemd/exordos-s3-agent.service" $SYSTEMD_SERVICE_DIR
sudo cp "$GC_PATH/etc/systemd/exordos-rustfs.service" $SYSTEMD_SERVICE_DIR

# Enable exordos s3 agent service
sudo systemctl enable exordos-s3-agent
