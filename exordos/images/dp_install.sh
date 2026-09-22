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


# Install packages
sudo apt update
sudo apt dist-upgrade -y
sudo apt install -y \
    libev-dev unzip

# Install rustfs binary. The version is pinned and checked: a "latest" archive
# silently moves (or silently doesn't), and both architectures must run the
# same release.
RUSTFS_VERSION="1.0.1-preview.10"
ARCH=$(uname -m)
case "$ARCH" in
    x86_64)
        RUSTFS_SHA256="baa6731ec86314fb766b909f1e6176f9190a356f73ebcd5a77623a631f6d212e"
        ;;
    aarch64)
        RUSTFS_SHA256="ebcf7dd1c53acfde71a6d4f3a2aa74715036b6bd8d538dda2b743de37e791078"
        ;;
    *)
        echo "Unsupported CPU architecture: $ARCH" >&2
        exit 1
        ;;
esac
RUSTFS_PKG="rustfs-linux-${ARCH}-gnu-v${RUSTFS_VERSION}.zip"
RUSTFS_PKG_URLS=(
    "https://dl.rustfs.com/artifacts/rustfs/release/${RUSTFS_PKG}"
    "https://github.com/rustfs/rustfs/releases/download/${RUSTFS_VERSION}/${RUSTFS_PKG}"
)

TMP_DIR=$(mktemp -d)
for url in "${RUSTFS_PKG_URLS[@]}"; do
    if curl -fL --retry 3 -o "$TMP_DIR/rustfs.zip" "$url" &&
        echo "$RUSTFS_SHA256  $TMP_DIR/rustfs.zip" | sha256sum -c -; then
        break
    fi
    rm -f "$TMP_DIR/rustfs.zip"
done
if [ ! -f "$TMP_DIR/rustfs.zip" ]; then
    echo "Can't download RustFS $RUSTFS_VERSION" >&2
    exit 1
fi
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
sudo install -m 0755 "$GC_PATH/exordos/images/sync_hosts.sh" /usr/local/bin/exordos-s3-sync-hosts

cd "$GC_PATH"
uv sync
source "$GC_PATH/.venv/bin/activate"

# Link the universal agent (loads the S3CapabilityDriver via entry point)
sudo ln -sf "$VENV_PATH/bin/exordos-universal-agent" "/usr/bin/exordos-universal-agent"

deactivate

# Install Systemd service files
sudo cp "$GC_PATH/etc/systemd/exordos-metapaas-s3-agent.service" $SYSTEMD_SERVICE_DIR
sudo cp "$GC_PATH/etc/systemd/exordos-metapaas-rustfs.service" $SYSTEMD_SERVICE_DIR

# The unit ordering keeps the agent behind persistent-storage bootstrap.
sudo systemctl enable exordos-metapaas-s3-agent
