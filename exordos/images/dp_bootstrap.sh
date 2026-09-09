#!/usr/bin/env bash

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

set -eu
set -x
set -o pipefail

BOOTSTRAP_LIB=${EXORDOS_BOOTSTRAP_LIB:-/usr/local/lib/exordos/lib_bootstrap.sh}
source "$BOOTSTRAP_LIB"

# persistent data routines (rustfs object data lives on the second disk)
PERSISTENT_DISK=$(find_persistent_disk)
prepare_persistent_disk "$PERSISTENT_DISK" "$PERSISTENT_MOUNT" "xfs"

if [[ -n "$PERSISTENT_DISK" ]]; then
    migrate_to_persistent_restart "/var/log" "${PERSISTENT_MOUNT}/var/log" "systemd-journald rsyslog"
    migrate_to_persistent_stop_start \
        "/var/lib/rustfs/data" \
        "${PERSISTENT_MOUNT}/var/lib/rustfs/data" \
        "exordos-metapaas-rustfs"
    persist_migrate_complete
fi

# RustFS is enabled after the persistent bind mount is installed and starts
# once the control plane delivers its configuration.
sudo systemctl enable --now exordos-metapaas-rustfs

echo "Bootstrap completed successfully."
