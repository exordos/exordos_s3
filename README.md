# S3 Storage PaaS Plugin for MetaPaaS

A production-ready S3-compatible object storage service (RustFS + MinIO) packaged as a MetaPaaS plugin.

## What is This?

This repository provides the **s3aas** plugin: a complete, installable PaaS service for Exordos MetaPaaS. Instead of deploying separate control-plane infrastructure for S3 storage, this plugin integrates with a shared MetaPaaS runtime.

**Key components:**
- **CP (Control Plane):** REST API for managing S3 instances, buckets, users, policies, access keys (Python)
- **DP (Data Plane):** RustFS-based S3-compatible object storage VM (Packer-built)
- **Integration:** Automatic installation + scaling via MetaPaaS orchestration

## Quick Start

### Prerequisites

- Running `exordos_core` deployment
- Running `exordos_metapaas` deployment (shares one control-plane)
- Local build tools: `exordos` CLI, `packer`, Python 3.10+

### Build

```bash
make build \
  REPOSITORY=http://10.20.0.1:8080/exordos-elements \
  INDEX_URL=http://10.20.0.1:8080/simple/
```

Produces:
- `output/dist/exordos_paas_s3-*.whl` — Control-plane package
- `output/exordos-metapaas-s3-dp.raw.zst` — Data-plane VM image
- `output/manifests/s3aas.yaml` — Element manifest for Exordos Core

### Publish to Index

```bash
make publish-wheel
# Copies .whl to /srv/exordos-local-repo/simple/
```

### Install to Exordos Core

```bash
exordos -e http://10.20.0.2:11010 \
  -u admin -p <admin-password> \
  ee install metapaas --version 0.0.7 --repository http://10.20.0.1:8080/exordos-elements

exordos -e http://10.20.0.2:11010 \
  -u admin -p <admin-password> \
  ee install s3aas --version 0.0.1 --repository http://10.20.0.1:8080/exordos-elements
```

Wait for both elements to become ACTIVE.

### Create an S3 Instance

```bash
curl -X POST http://metapaas-cp:8080/v1/types/s3/instances \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <token>' \
  -d '{
    "name": "s3-data",
    "version": "0.0.1",
    "bucket_name": "data-bucket",
    "encryption": "aes256",
    "versioning": true
  }'
```

Response (example):
```json
{
  "id": "s3-instance-uuid",
  "name": "s3-data",
  "status": "CREATING",
  "bucket_name": "data-bucket",
  "encryption": "aes256",
  "versioning": true,
  "nodes": [
    {
      "id": "node-uuid",
      "ip": "10.20.0.21",
      "port": 9000
    }
  ]
}
```

Wait for `status` to become `ACTIVE`, then access via boto3:

```python
import boto3

s3 = boto3.client(
    's3',
    endpoint_url='http://10.20.0.21:9000',
    aws_access_key_id='<access-key>',
    aws_secret_access_key='<secret-key>',
    region_name='us-east-1'
)

s3.put_object(Bucket='data-bucket', Key='test.txt', Body=b'hello')
obj = s3.get_object(Bucket='data-bucket', Key='test.txt')
print(obj['Body'].read())  # b'hello'
```

## API Reference

### Instances

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/v1/types/s3/instances` | Create instance |
| GET | `/v1/types/s3/instances` | List instances |
| GET | `/v1/types/s3/instances/{id}` | Get instance details |
| PATCH | `/v1/types/s3/instances/{id}` | Update configuration |
| DELETE | `/v1/types/s3/instances/{id}` | Delete instance |

### Instance Fields

| Field | Type | Mutable | Description |
|-------|------|--------|-------------|
| `name` | string | ✓ | Instance name (unique per project) |
| `version` | string | ✗ | PaaS version (e.g., "0.0.1") |
| `bucket_name` | string | ✗ | Default bucket name |
| `encryption` | string | ✓ | Encryption mode: "none" or "aes256" |
| `versioning` | boolean | ✓ | Enable S3 object versioning |
| `mfa_delete` | boolean | ✓ | Require MFA to delete objects |
| `status` | string | ✗ | Instance status: PENDING, CREATING, ACTIVE, ERROR |
| `nodes` | array | ✗ | Data-plane nodes with IP, port |

### Buckets

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/v1/types/s3/instances/{id}/buckets` | Create bucket |
| GET | `/v1/types/s3/instances/{id}/buckets` | List buckets |
| DELETE | `/v1/types/s3/instances/{id}/buckets/{name}` | Delete bucket |

### Users

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/v1/types/s3/instances/{id}/users` | Create IAM user |
| GET | `/v1/types/s3/instances/{id}/users` | List users |
| DELETE | `/v1/types/s3/instances/{id}/users/{name}` | Delete user |

### Access Keys

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/v1/types/s3/instances/{id}/users/{user}/access-keys` | Create key pair |
| GET | `/v1/types/s3/instances/{id}/users/{user}/access-keys` | List keys |
| DELETE | `/v1/types/s3/instances/{id}/users/{user}/access-keys/{key}` | Delete key |

## Testing

### Unit Tests

```bash
make test
# or: tox -e py312
```

### Lint & Type Checking

```bash
make lint      # ruff check
make format    # ruff format
make typecheck # mypy
```

### Functional Tests

Requires live exordos_core + exordos_metapaas deployment:

```bash
EXORDOS_ENDPOINT=http://10.20.0.2:11010 \
EXORDOS_USERNAME=admin \
EXORDOS_PASSWORD=<pass> \
METAPAAS_USERNAME=metapaas \
METAPAAS_PASSWORD=<pass> \
EXORDOS_S3_CP_URL=http://10.20.0.X:8080 \
make functional
```

Or use the E2E preparation script:

```bash
python exordos_paas_s3/tests/functional/prepare_env.py \
  --metapaas-dir ../exordos_metapaas \
  --project-dir . \
  --output-dir /tmp/s3-build \
  --endpoint http://10.20.0.2:11010 \
  --username admin \
  --password <admin-pass> \
  --wait-timeout 600
```

This script:
1. Builds s3aas + metapaas elements
2. Serves them via HTTP
3. Installs both to running exordos_core
4. Outputs environment variables for tests
5. Waits for all nodes ACTIVE

## Architecture

### Control Plane (exordos_paas_s3/)

- **models.py** — SQLAlchemy: S3Instance, Bucket, User, Policy, AccessKey, Version
- **controllers.py** — REST endpoints with field-level IAM permissions
- **iam_config.py** — IAM roles: owner, operator, viewer
- **migrations/** — Database schema migrations (per-paas independent)
- **tests/** — Unit + functional test suites

### Data Plane (exordos/s3-dp/)

- **packer.pkr.hcl** — Builds RustFS-based S3 VM image
- **conf/** — Configuration templates (rendered per-instance)
- **systemd/** — Service units (rustfs, minio-gateway, etc.)

### Manifest & Build

- **exordos/exordos.yaml** — Build config (orchestrates CP wheel + DP image + manifest)
- **exordos/manifests/s3aas.yaml.j2** — Jinja2 element manifest (deployed to exordos_core)

### CI/CD

- **.github/workflows/tests.yaml** — Unit tests on Python 3.10/3.12/3.13
- **.github/workflows/func_tests.yaml** — Full E2E: bootstrap core → build → install → test

## Repository Structure

```
.
├── exordos_paas_s3/              # Control-plane Python package
│   ├── models.py                 # SQLAlchemy ORM models
│   ├── controllers.py            # REST API controllers
│   ├── iam_config.py             # IAM setup
│   ├── migrations/               # Database migrations
│   └── tests/
│       ├── test_*.py             # Unit tests
│       └── functional/           # E2E tests
├── exordos/
│   ├── exordos.yaml              # Build config
│   ├── manifests/
│   │   └── s3aas.yaml.j2         # Element manifest template
│   └── s3-dp/
│       └── packer.pkr.hcl        # Data-plane image config
├── pyproject.toml                # Project metadata + dependencies
├── tox.ini                       # Test automation
├── Makefile                      # Build targets
├── .github/workflows/            # CI/CD pipelines
├── README.md                     # This file
└── output/                       # Build artifacts (git-ignored)
    ├── dist/                     # Python wheel
    ├── manifests/                # Rendered manifests
    └── images/                   # Packer-built images
```

## Configuration

### Build Manifest Variables

Control how the element image and manifest are rendered:

```bash
exordos build \
  --manifest-var repository=http://custom-repo.local/exordos-elements \
  --manifest-var index_url=http://custom-repo.local/simple/
```

These override defaults in `exordos/exordos.yaml`.

### Instance Configuration

When creating an S3 instance, specify:

```json
{
  "name": "my-s3",
  "version": "0.0.1",
  "bucket_name": "default-bucket",
  "encryption": "aes256",
  "versioning": true,
  "mfa_delete": false
}
```

Configuration is applied to all data-plane nodes in the instance (currently single-node; clustering planned).

## Troubleshooting

### Instance stuck in CREATING

Check PluginReconciler logs on metapaas-cp:

```bash
exordos -e http://10.20.0.2:11010 -u admin -p <pass> \
  cn exec metapaas-cp -- \
  journalctl -u metapaas-plugin-reconciler -f
```

Also check orchestration logs:

```bash
exordos -e http://10.20.0.2:11010 -u admin -p <pass> \
  cn exec metapaas-cp -- \
  tail -f /var/log/orch-api.log
```

### Cannot connect to S3 API

Verify data-plane node is ACTIVE and reachable:

```bash
# Get node IP from instance
curl -s http://metapaas-cp:8080/v1/types/s3/instances/<id> \
  -H 'Authorization: Bearer <token>' | jq '.nodes[].ip'

# Test connectivity
telnet <ip> 9000

# Check S3 health
curl -s http://<ip>:9000/health
```

### Permissions error (403 Forbidden)

Ensure test user has `owner` role in METAPAAS_PROJECT_ID:

```bash
# Query user's roles
curl -s http://10.20.0.2:11010/v1/iam/projects/4d657461-0000-0000-0000-000000000002/roles \
  -u admin:<pass> | jq '.items[] | select(.user_id=="<your-uuid>")'
```

## Development Guidelines

### Adding New Endpoints

1. Add model field to `models.py`
2. Add controller method + permissions to `controllers.py`
3. Add migration in `migrations/` with UUID name
4. Test in `tests/functional/`

### Read-Only Fields

Fields like `status`, `id`, `created_at`, `nodes`, `kind` are read-only and auto-populated. Omit them from CREATE payloads.

### IAM Model

Permissions are bound to **METAPAAS_PROJECT_ID**, not instance project. Users must have explicit role grant in metapaas project to access any S3 instance.

## References

- **MetaPaaS Platform:** [../exordos_metapaas/](../exordos_metapaas/)
- **How to Build New PaaS:** [../exordos_metapaas/HOW_TO_BUILD_NEW_PAAS.md](../exordos_metapaas/HOW_TO_BUILD_NEW_PAAS.md)
- **Mail-aas Blueprint:** [../exordos_mail/](../exordos_mail/) (reference plugin, simpler than s3aas)
- **Exordos Core:** https://github.com/exordos/exordos_core

## License

Proprietary — Exordos
