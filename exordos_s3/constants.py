#    Copyright 2025-2026 Genesis Corporation.
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

# Path on the dataplane (rustfs) node where the control plane delivers the
# rustfs environment config (must match the rustfs systemd unit's
# EnvironmentFile in the dataplane image).
RUSTFS_ENV_FILE = "/etc/exordos_metapaas/rustfs.env"

RUSTFS_PORT = 9000
RUSTFS_DATA_DIR = "/var/lib/rustfs/data"
# Readiness probe of the local node: 200 only once it serves S3 requests
RUSTFS_READY_URL = f"http://127.0.0.1:{RUSTFS_PORT}/health/ready"

# Nodes of a distributed instance reach each other by these names, resolved
# through /etc/hosts. RustFS identifies a pool by the literal endpoint string,
# so the names have to outlive node address changes.
CLUSTER_HOST_TEMPLATE = "node{ordinal}.rustfs.internal"
