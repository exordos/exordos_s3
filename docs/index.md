# Genesis S3

## Overview

Genesis S3 is an S3-compatible object storage service (S3aaS) built on top of RustFS. It provides scalable, managed object storage with bucket, policy, and user management via a REST API.

## Key Features

- S3-compatible API (RustFS)
- Bucket management with versioning, object lock, and public access
- Fine-grained IAM policies with user/access key separation
- Automatic infrastructure provisioning and reconciliation
- Multi-node support

## Architecture

```mermaid
graph TD
    %% Main S3 Instance entity
    S3[S3 Instance]

    %% Infrastructure layer
    NS[Node Set]
    N1[Node]

    %% S3 components
    B1[Bucket]
    B2[Bucket]
    P1[Policy]
    P2[Policy]
    U1[User]
    U2[User]
    AK1[Access Key]
    AK2[Access Key]
    ATT1[User-Policy Attachment]

    %% Relationships
    S3 -->|"manages IaaS (internal)"| NS
    NS -->|contains| N1
    S3 -->|contains| B1
    S3 -->|contains| B2
    S3 -->|contains| P1
    S3 -->|contains| P2
    S3 -->|contains| U1
    S3 -->|contains| U2
    U1 -->|owns| AK1
    U2 -->|owns| AK2
    U1 -->|attached| P1
    ATT1 -->|links| U1
    ATT1 -->|links| P1

    %% Legend
    class S3 main
    class NS,N1 component
    class B1,B2 bucket
    class P1,P2 policy
    class U1,U2 user
    class AK1,AK2 accesskey
```

The platform consists of:

- **Control Plane (CP)**: User API, orchestration API, PaaS/Infra builders
- **Data Plane (DP)**: RustFS instance on a VM, agent with reconciliation loop
- **Agent**: Syncs target state from CP to actual state on RustFS via admin API
