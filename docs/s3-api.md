# S3 Service API

## Overview

The S3 service provides a REST-based API for creating and managing S3-compatible object storage instances. Each instance runs a dedicated RustFS node with full bucket, policy, and user management.

## Core Components

- `S3 Instance`: A logical S3 storage instance with infrastructure
- `Bucket`: Object storage bucket with versioning, object lock, and access control
- `Policy`: IAM-style access policy (JSON document)
- `User`: Logical user grouping for policy attachment
- `Access Key`: S3 access key (access_key/secret_key pair) — the actual RustFS user

### S3 Instance

The main instance entity that manages:

- Status (NEW, IN_PROGRESS, ACTIVE, ERROR)
- Configuration parameters:
    - CPU cores (1-128)
    - RAM (512MB-1TB)
    - Disk size (8GB-1TB)
    - Node count (1-16, currently single_node only)
- Version information (RustFS image)
- Root secret (auto-generated, hidden in API)
- Associated buckets, policies, users, and access keys

### Bucket

Object storage buckets within the S3 instance:

- Name validation (3-63 characters, DNS-compliant)
- Versioning (enabled/disabled, read-only after creation)
- Object lock with retention mode (GOVERNANCE/COMPLIANCE) and retention days
- Public read access toggle
- Quota (reserved for future use)

### Policy

IAM-style access policies:

- JSON policy document (AWS S3 policy format)
- Builtin policies cannot be deleted
- Policies are attached to users via User-Policy Attachment

### User

Logical user grouping — a container for access keys and policies:

- Users are **not** created on the data plane directly
- They group access keys and define which policies apply to them
- All access keys under a user inherit its policies

### Access Key

S3 credential pair used for authentication:

- `access_key`: Auto-generated 20-character identifier (read-only after creation)
- `secret_key`: Auto-generated 40-character secret (hidden in API responses)
- On the data plane, each access key becomes a RustFS user with the parent user's policies

### Internal (not visible to user)

#### Node Set

Infrastructure layer that manages the underlying compute resources:

- Root disk with RustFS image
- Data disk for object storage
- RustFS configuration delivered via Config resource

## API Structure

### Creating an S3 Instance

```json
{
  "name": "production-s3",
  "description": "Production S3 storage",
  "cpu": 4,
  "ram": 4096,
  "disk_size": 100,
  "nodes_number": 1,
  "kind": "single_node",
  "version": "/v1/types/s3/versions/VERSION_UUID"
}
```

### Creating a Bucket

```json
{
  "name": "my-bucket",
  "description": "Application data bucket",
  "versioning_enabled": true,
  "object_lock_enabled": false,
  "public": false
}
```

### Creating a Policy

```json
{
  "name": "read-only-bucket",
  "description": "Read-only access to a specific bucket",
  "content": {
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": ["s3:GetObject", "s3:ListBucket"],
        "Resource": ["arn:aws:s3:::my-bucket", "arn:aws:s3:::my-bucket/*"]
      }
    ]
  }
}
```

### Creating a User

```json
{
  "name": "app-user",
  "description": "Application user"
}
```

### Creating an Access Key

```json
{
  "name": "app-key",
  "description": "Application access key"
}
```

**Response** (secret_key is only visible at creation time):

```json
{
  "uuid": "key-uuid",
  "access_key": "ABCDEFGHIJKLMNOPQRST",
  "secret_key": "abcdefghijklmnopqrstuvwxyz1234567890ABCD",
  "status": "ACTIVE",
  "user": "/v1/types/s3/instances/INSTANCE_UUID/users/USER_UUID"
}
```

### Attaching a Policy to a User

```json
{
  "user": "/v1/types/s3/instances/INSTANCE_UUID/users/USER_UUID",
  "policy": "/v1/types/s3/instances/INSTANCE_UUID/policies/POLICY_UUID"
}
```

## Validation Rules

### Instance Validation

- CPU must be between 1 and 128 cores
- RAM must be between 512MB and 1TB
- Disk size must be between 8GB and 1TB
- Node count must be between 1 and 16
- `single_node` kind requires `nodes_number=1`
- Disk size shrink is not supported

### Bucket Validation

- Name must be 3-63 characters (DNS-compliant)
- `versioning_enabled` and `object_lock_enabled` are read-only after creation
- `default_retention_mode` must be GOVERNANCE or COMPLIANCE (if set)
- `default_retention_days` must be 1-365000 (if set)

### Policy Validation

- `content` must be a valid JSON policy document
- Builtin policies cannot be deleted

## Status Management

### Instance Status Lifecycle

1. **NEW**: Instance created, infrastructure provisioning started
2. **IN_PROGRESS**: Infrastructure being provisioned, RustFS being installed
3. **ACTIVE**: Instance ready for use
4. **ERROR**: Provisioning or configuration failed

### Component Status

- Buckets: ACTIVE / ERROR
- Policies: ACTIVE / ERROR
- Users: ACTIVE / ERROR
- Access Keys: ACTIVE / ERROR

## Data Plane Reconciliation

The agent on each RustFS node reconciles target state (from CP) with actual state on RustFS:

1. **Policies**: Create/update/delete RustFS canned policies
2. **Users**: Create access keys as RustFS users with parent user's policies; remove stale users
3. **Buckets**: Create/delete buckets; apply versioning, object lock, and public access settings

### Key Design Decisions

- Access keys are created as **regular RustFS users** (not service accounts), because RustFS service accounts always inherit policies from the admin signing key
- Parent users from the CP model are **logical groupings only** — they are not created on the data plane
- Each access key user in RustFS gets the policies of its parent CP user attached directly

## API Endpoints

### Instance Management

- `POST /v1/types/s3/instances` - Create new instance
- `GET /v1/types/s3/instances` - List instances
- `GET /v1/types/s3/instances/{uuid}` - Get instance details
- `PUT /v1/types/s3/instances/{uuid}` - Update instance
- `DELETE /v1/types/s3/instances/{uuid}` - Delete instance

### Bucket Management

- `POST /v1/types/s3/instances/{uuid}/buckets` - Create bucket
- `GET /v1/types/s3/instances/{uuid}/buckets` - List buckets
- `GET /v1/types/s3/instances/{uuid}/buckets/{uuid}` - Get bucket
- `PUT /v1/types/s3/instances/{uuid}/buckets/{uuid}` - Update bucket
- `DELETE /v1/types/s3/instances/{uuid}/buckets/{uuid}` - Delete bucket

### Policy Management

- `POST /v1/types/s3/instances/{uuid}/policies` - Create policy
- `GET /v1/types/s3/instances/{uuid}/policies` - List policies
- `GET /v1/types/s3/instances/{uuid}/policies/{uuid}` - Get policy
- `PUT /v1/types/s3/instances/{uuid}/policies/{uuid}` - Update policy
- `DELETE /v1/types/s3/instances/{uuid}/policies/{uuid}` - Delete policy

### User Management

- `POST /v1/types/s3/instances/{uuid}/users` - Create user
- `GET /v1/types/s3/instances/{uuid}/users` - List users
- `GET /v1/types/s3/instances/{uuid}/users/{uuid}` - Get user
- `DELETE /v1/types/s3/instances/{uuid}/users/{uuid}` - Delete user

### Access Key Management

- `POST /v1/types/s3/instances/{uuid}/users/{uuid}/keys` - Create access key
- `GET /v1/types/s3/instances/{uuid}/users/{uuid}/keys` - List access keys
- `GET /v1/types/s3/instances/{uuid}/users/{uuid}/keys/{uuid}` - Get access key
- `DELETE /v1/types/s3/instances/{uuid}/users/{uuid}/keys/{uuid}` - Delete access key

### User-Policy Attachment

- `POST /v1/types/s3/instances/{uuid}/users/{uuid}/policies` - Attach policy
- `GET /v1/types/s3/instances/{uuid}/users/{uuid}/policies` - List attachments
- `DELETE /v1/types/s3/instances/{uuid}/users/{uuid}/policies/{uuid}` - Detach policy

### Version Management

- `GET /v1/types/s3/versions` - List all available versions
- `GET /v1/types/s3/versions/{uuid}` - Get specific version details
