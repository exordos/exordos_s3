# Genesis S3aaS

## Overview
Genesis S3 is an S3-compatible Object Storage as a Service platform. It provides managed, scalable object storage instances powered by RustFS with full S3 API compatibility.

## Key Features
- S3-compatible API (works with any S3 client/SDK)
- Managed RustFS instances with configurable resources
- Bucket management with versioning, quotas, object lock and public access
- IAM-compatible policies (builtin + custom JSON)
- Access key management with rotation support

## Architecture
The platform consists of:
- User API (Management Control Plane)
- IaaS entities orchestration
- PaaS entities orchestration
- Data Plane (RustFS instances)
