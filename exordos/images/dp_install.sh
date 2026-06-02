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
set -x
set -o pipefail


GC_PATH="/opt/exordos_metapaas"
GC_CFG_DIR=/etc/exordos_metapaas
WORK_DIR="/var/lib/exordos/exordos_metapaas"
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
        RUSTFS_PKG_URL="https://dl.rustfs.com/artifacts/rustfs/release/rustfs-linux-x86_64-gnu-latest.zip"
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
sudo mkdir -p $WORK_DIR
sudo mkdir -p $RUSTFS_DATA_DIR

# Install agent config + bootstrap
sudo cp "$GC_PATH/etc/exordos_metapaas/metapaas_s3_agent.conf" $GC_CFG_DIR/
sudo cp "$GC_PATH/etc/exordos_metapaas/logging.yaml" $GC_CFG_DIR/
sudo cp "$GC_PATH/exordos/images/dp_bootstrap.sh" $BOOTSTRAP_PATH/0100-metapaas-s3-dp-bootstrap.sh
sudo chmod +x $BOOTSTRAP_PATH/0100-metapaas-s3-dp-bootstrap.sh

cd "$GC_PATH"
uv sync
source "$GC_PATH/.venv/bin/activate"

# In the dev mode the gcl_sdk package is installed from the local machine
if [[ "$SDK_DEV_MODE" == "true" ]]; then
    uv pip uninstall -y gcl_sdk
    uv pip install -e "$DEV_SDK_PATH"
fi

# Link the universal agent (loads the S3CapabilityDriver via entry point)
sudo ln -sf "$VENV_PATH/bin/exordos-universal-agent" "/usr/bin/exordos-universal-agent"

deactivate

# Install Systemd service files
sudo cp "$GC_PATH/etc/systemd/exordos-metapaas-s3-agent.service" $SYSTEMD_SERVICE_DIR
sudo cp "$GC_PATH/etc/systemd/exordos-metapaas-rustfs.service" $SYSTEMD_SERVICE_DIR

# Enable the dataplane agent (rustfs is enabled by bootstrap after the config
# is delivered by the control plane)
sudo systemctl enable exordos-metapaas-s3-agent
