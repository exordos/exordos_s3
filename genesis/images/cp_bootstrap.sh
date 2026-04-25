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

GC_PATH="/opt/genesis_s3"
SERVICE_CONFIG="/etc/genesis_s3/genesis_s3.conf"
CORE_AGENT_CONFIG="/etc/genesis_s3/core_agent.conf"

while [ ! -f /etc/genesis_init.txt ]; do sleep 1; done
source /etc/genesis_init.txt

export IAM_USER_NAME="${IAM_USER_NAME:-genesis_s3}"
export IAM_USER_PASS="${IAM_USER_PASS:-genesis_s3}"
export PROJECT_ID="${PROJECT_ID}"
export GC_HS256_JWKS_ENCRYPTION_KEY="${GC_HS256_JWKS_ENCRYPTION_KEY:-}"

export GC_PG_USER="${GC_PG_USER:-genesis_s3}"
export GC_PG_PASS="${GC_PG_PASS:-genesis_s3}"
export GC_PG_DB="${GC_PG_DB:-genesis_s3}"
export GC_PG_ENDPOINTS="${GC_PG_ENDPOINTS:-}"

# Wait for GC_PG_ENDPOINTS to be available
while [ -z "$GC_PG_ENDPOINTS" ]; do
    echo "GC_PG_ENDPOINTS is empty, re-reading genesis_init.txt..."
    sleep 5
    source /etc/genesis_init.txt
    export GC_PG_ENDPOINTS="${GC_PG_ENDPOINTS:-}"
done

# Generate config from template, if it doesn't exist
try_generate_config() {
    local config_file="$1"
    local config_template="${config_file}.j2"

    if [[ -f "$config_file" ]]; then
        echo "Config file $config_file already exists, do nothing"
        return 0
    fi

    if [[ ! -f "$config_template" ]]; then
        echo "ERROR: Config template $config_template not found"
        return 1
    fi

    j2 "$config_template" -o "$config_file"

    echo "Config file created at $config_file"
}

if [[ ! -f $SERVICE_CONFIG ]]; then
    try_generate_config "$SERVICE_CONFIG"
fi

if [[ ! -f $CORE_AGENT_CONFIG ]]; then
    try_generate_config "$CORE_AGENT_CONFIG"
fi

sudo mkdir -p /var/lib/genesis/genesis_s3/core_agent

# Wait for database to be available
wait_for_db() {
    local attempt=1

    echo "Waiting for database to be available (infinite wait)..."

    while true; do
        # Try to connect to database using psql
        if PGPASSWORD="$GC_PG_PASS" psql -h "$GC_PG_ENDPOINTS" -U "$GC_PG_USER" -d "$GC_PG_DB" -c "SELECT 1;" >/dev/null 2>&1; then
            echo "Database is available after $attempt attempts"
            return 0
        fi

        echo "Attempt $attempt: Database not ready, waiting 5 seconds..."
        sleep 5
        attempt=$((attempt + 1))
    done
}

wait_for_db

source "$GC_PATH"/.venv/bin/activate
ra-apply-migration --config-dir "/etc/genesis_s3/" --path "$GC_PATH/.venv/lib/python3.12/site-packages/gcl_sdk/migrations"
ra-apply-migration --config-dir "/etc/genesis_s3/" --path "$GC_PATH/migrations"
deactivate

# Enable genesis s3 services
sudo systemctl enable --now \
    genesis-s3-gservice \
    genesis-s3-user-api \
    genesis-s3-status-api \
    genesis-s3-orch-api \
    genesis-s3-core-agent

echo "Bootstrap completed successfully."
