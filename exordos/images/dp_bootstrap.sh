#!/usr/bin/env bash

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

set -eu
set -x
set -o pipefail

source /usr/local/lib/exordos/lib_bootstrap.sh

# persistent data routines
PERSISTENT_DISK=$(find_persistent_disk)

# Partial copy of prepare_persistent_disk to use XFS instead of EXT4
partition=$(get_partition_name "$PERSISTENT_DISK")

# Check if partition already exists
if has_gpt_partition "$PERSISTENT_DISK"; then
    echo "GPT partition already exists on $PERSISTENT_DISK"
else
    # Disk is not partitioned, create GPT partition table and partition
    create_gpt_partition "$PERSISTENT_DISK"
    echo "Formatting persistent partition $partition with XFS..."
    mkfs.xfs "$partition"
fi

prepare_persistent_disk "$PERSISTENT_DISK" "$PERSISTENT_MOUNT"

if [[ -n "$PERSISTENT_DISK" ]]; then
    # Migrate logs first, some processes may be left writing to root disk until next reboot
    migrate_to_persistent_restart "/var/log" "${PERSISTENT_MOUNT}/var/log" "systemd-journald rsyslog"

    # Migrate rustfs data
    migrate_to_persistent "/var/lib/rustfs/data" "${PERSISTENT_MOUNT}/var/lib/rustfs/data"

    persist_migrate_complete
fi

# Enable exordos s3 services
sudo systemctl enable --now \
    exordos-rustfs

echo "Bootstrap completed successfully."
