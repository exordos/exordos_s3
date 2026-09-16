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

# Keep the RustFS cluster host names in /etc/hosts.
#
# EXORDOS_S3_HOSTS comes from rustfs.env as comma separated "<ip>=<name>"
# pairs. The managed block is replaced as a whole and removed when the
# variable is empty (a single node instance).

set -eu
set -o pipefail

HOSTS_FILE=${EXORDOS_S3_HOSTS_FILE:-/etc/hosts}
BEGIN_MARK="# BEGIN exordos-s3 cluster"
END_MARK="# END exordos-s3 cluster"

tmp=$(mktemp "${HOSTS_FILE}.XXXXXX")
trap 'rm -f "$tmp"' EXIT

awk -v begin="$BEGIN_MARK" -v end="$END_MARK" '
    $0 == begin { skip = 1; next }
    $0 == end { skip = 0; next }
    !skip
' "$HOSTS_FILE" > "$tmp"

if [[ -n "${EXORDOS_S3_HOSTS:-}" ]]; then
    {
        echo "$BEGIN_MARK"
        tr ',' '\n' <<< "$EXORDOS_S3_HOSTS" | awk -F= 'NF == 2 { print $1 " " $2 }'
        echo "$END_MARK"
    } >> "$tmp"
fi

if cmp -s "$tmp" "$HOSTS_FILE"; then
    exit 0
fi

chmod --reference="$HOSTS_FILE" "$tmp"
mv "$tmp" "$HOSTS_FILE"
