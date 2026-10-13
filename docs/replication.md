# Bucket replication

[Русская версия](replication.ru.md)

Exordos manages RustFS bucket replication through separate **targets** and
**rules**. A source bucket can have several targets; each target can have several
rules with different prefixes, tags and deletion settings. Destination storage
can be another RustFS or a compatible S3 service. It must support versioning and
the replication requests used by RustFS; S3 compatibility alone does not prove
that versions and metadata will be preserved. The integration test uses RustFS
1.0.1 on both sides.

Replication copies object changes, not the Exordos database, IAM users, access
keys, policies or instance configuration. Preserve those separately for restore.
Propagating permanent version deletions can destroy backup history.
Use independent destination credentials and retention/version lifecycle settings
that preserve the recovery window you need.

## API paths

All identifiers in these paths are resource UUIDs, including the source bucket.
The authentication and project selection are the same as for other S3 resources.

| Collection | Path |
| --- | --- |
| Targets | `/v1/types/s3/instances/{instance}/buckets/{bucket}/replication_targets` |
| Rules | `/v1/types/s3/instances/{instance}/buckets/{bucket}/replication_targets/{target}/rules` |

Both collections accept `POST` and `GET`. Append `/{uuid}` for `GET`, `PUT`
(partial update) and `DELETE`. Resources have the usual `uuid`, `name`,
`description`, `project_id`, `created_at` and `updated_at` fields.

The IAM permissions are `exordos_s3.replication_target.{create,read,update,delete}`
and `exordos_s3.replication_rule.{create,read,update,delete}`. The service manifest
binds them to the existing project owner role. A target belongs to the source
bucket's project; a rule belongs to its target's project.

## Target fields

| Field | Default | Meaning |
| --- | --- | --- |
| `endpoint` | required | HTTP(S) origin, e.g. `https://backup.example:9000`; no path, URL credentials, query or fragment |
| `destination_bucket` | required | Existing destination bucket name |
| `access_key` | required | Destination access key; hidden from GET/list/filter responses |
| `secret_key` | required | Destination secret; hidden from GET/list/filter responses |
| `session_token` | `""` | Optional temporary credential token; hidden from GET/list/filter responses |
| `region` | `"us-east-1"` | Destination signing region |
| `bucket_lookup` | `"auto"` | `auto`, `path` or `dns`; `path` is useful for endpoints without virtual host routing |
| `ca_cert_pem` | `""` | Custom PEM trust certificate(s), for HTTPS only |
| `storage_class` | `"STANDARD"` | Destination object storage class |
| `bandwidth_limit` | `0` | Bytes/second; zero means unlimited |
| `synchronous` | `false` | RustFS native synchronous replication mode |
| `disable_proxy` | `true` | Disable RustFS proxy reads from this target |

TLS certificate verification is enabled. Loopback, link-local, multicast and
unspecified IP literals and `localhost` origins are rejected. Private network
origins are allowed. Users choose endpoints without an operator allowlist.
The destination must be reachable from the S3 nodes. URL validation does not
check IP addresses returned by DNS; operators provide network egress isolation
for S3 nodes from infrastructure networks, localhost and metadata services.

`endpoint`, `destination_bucket`, `region` and `storage_class` are immutable.
To change them, remove the rules and target and create a new target. Other fields
can be updated, including credentials. RustFS allows only one target with the
same endpoint authority (host and port) per source bucket, even if the
destination bucket differs. Endpoint host spelling and default ports are
normalized by the API.

Destination credentials are stored in the control-plane database and sent to the
S3 agent so RustFS can authenticate to the destination. `secret_key` and
`session_token` are excluded from agent reports, agent metadata and the replication
journal. POST/PUT responses can contain credentials; handle those responses as
sensitive. RustFS stores its own remote credentials. This feature does not
encrypt objects for an untrusted receiver.

## Rule fields

| Field | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Enable this rule; `false` pauses it without deleting the target |
| `priority` | `1` | Positive integer, unique across all rules of the source bucket |
| `prefix` | `""` | Match object key prefix; empty matches every key |
| `tags` | `{}` | Match all specified object tags; combined with prefix using AND |
| `replicate_existing` | `true` | Enable RustFS replication of existing eligible objects |
| `replicate_delete_markers` | `true` | Propagate versioned delete markers |
| `replicate_deletes` | `false` | Propagate deletion of specific object versions |

Replica-side metadata modifications are not replicated (`ReplicaModifications`
is always `Disabled`). Metadata changes on original source objects are replicated
normally.

A bucket supports at most 1000 Exordos rules. Tag keys contain 1–128 characters;
tag values contain at most 256. Tag filters require explicitly setting
`replicate_delete_markers=false`; otherwise the rule is rejected. Multiple targets
require distinct priorities too.

RustFS copies existing objects through its background scanner. Enabling a rule
does not mean all existing objects have already been copied; completion time
depends on the bucket size and scanner settings.

By default, an ordinary source DELETE creates a marker at the destination too:
the object disappears from ordinary GETs and listings while previous versions
remain available. Restore deleted objects by selecting a version ID. Set
`replicate_delete_markers=false` to keep deleted objects visible to ordinary GETs.
Leave `replicate_deletes=false` to preserve destination versions when those
versions are deleted at the source. Enabling it allows destruction of destination
history by source version deletion. Destination lifecycle expiration is a
separate mechanism.
Native synchronous mode does not by itself constitute a measured RPO or a
retention guarantee.

## Example

Create the destination bucket and enable its versioning using its own S3 API.
The destination credentials need bucket/versioning access for registration and
object/version/metadata operations for the selected replication rules. Exact IAM
permissions depend on the receiver. Object lock and retention are configured on
the destination separately.

Enable versioning on the existing source bucket:

```http
PUT /v1/types/s3/instances/{instance}/buckets/{bucket}
Content-Type: application/json

{"versioning_enabled": true}
```

Versioning cannot be suspended through this API while replication targets exist
or Object Lock is enabled on the bucket.
Create a target, keeping its returned UUID:

```http
POST /v1/types/s3/instances/{instance}/buckets/{bucket}/replication_targets
Content-Type: application/json

{
  "name": "offsite",
  "endpoint": "https://backup.example:9000",
  "destination_bucket": "archive",
  "access_key": "replication-writer",
  "secret_key": "<destination secret>",
  "bucket_lookup": "path",
  "bandwidth_limit": 10485760
}
```

Create a rule for that target:

```http
POST /v1/types/s3/instances/{instance}/buckets/{bucket}/replication_targets/{target}/rules
Content-Type: application/json

{
  "name": "photos",
  "priority": 10,
  "prefix": "photos/",
  "tags": {"backup": "yes"},
  "replicate_existing": true,
  "replicate_delete_markers": false,
  "replicate_deletes": false
}
```

Add a second rule with priority 20 for another prefix, or a second target with a
different endpoint and rules with unused priorities. To replicate the whole
bucket, use an empty prefix and empty tags.

Pause a rule with `PUT .../rules/{rule}` and `{"enabled": false}`. Rotate keys
with `PUT .../replication_targets/{target}` and `{"access_key": "new-key",
"secret_key": "<new secret>"}`. Updating the keys retains the native target ARN.

Delete a target's rules before deleting the target. Deleting the source bucket
or instance also removes its Exordos target/rule resources. Removing replication
configuration does not delete the destination bucket or its objects.

## Reconciliation and operation

The S3 agent registers targets through the RustFS admin API and applies the S3
replication XML. One node reconciles a distributed instance. Its journal keeps
UUID-to-ARN bindings, pending registration intents and credential revisions for
retries. Pending intents contain public target settings, without secrets.
Rule IDs are
`exordos:{rule-uuid}`; Exordos owns this namespace. Rules with other IDs and their
XML extensions are preserved. Foreign priority collisions, conflicting nonempty
Role settings and an already registered unmanaged target at the same endpoint
cause reconciliation to fail without overwriting the foreign configuration.

Upgrade the S3 agents before enabling replication through a newer control plane.
An unconfigured bucket/instance keeps the previous node target fields and hash;
older agents can still settle while the feature is unused. The migration nudges
existing instances to rebuild their target resources. New agents can read legacy
metadata. Before suspending source versioning, the agent removes obsolete
replication configuration so RustFS's native versioning guard can succeed.

Before rolling back the plugin or its migration, delete its rules and targets
and wait for the agents to remove the managed RustFS configuration. Verify there
are no `exordos:` rules or journal bindings left. Migration downgrade refuses
while target rows exist; an empty CP database alone does not prove DP cleanup
has completed. Rolling back the code before cleanup would leave replication
running without CP management.

Changes are asynchronous. API success means the desired configuration was saved;
it does not prove that objects have arrived. The agent compares actual settings
on subsequent iterations and retries failed application. A configuration mismatch
prevents the instance from settling; check the S3 agent log and RustFS replication
metrics for the affected source bucket. There is no per-rule progress/RPO API.
The [monitoring guide](monitoring.md#bucket-replication) describes these metrics
and the dashboard's Replication section.

Do not edit Exordos rule IDs or remote targets manually. A lost journal can be
recovered from owned rules that reference the native ARN. Pending registrations
are recorded before the remote request;
a retry with its journal can recover after losing the response by comparing the
pending public target settings. A lost journal and a target with no owned rule
require operator intervention. Exordos refuses to adopt an existing endpoint
without an owned rule or a matching pending intent. Coordinate external admin
changes with Exordos: redacted secrets cannot distinguish simultaneous identical
registrations. A foreign rule that references a removed managed target also
prevents that target from being deleted. Secrets changed outside Exordos cannot
be verified through RustFS's redacted listing.

To run the isolated backend test:

```sh
EXORDOS_TEST_RUSTFS_BINARY=/path/to/rustfs-1.0.1 \
  .tox/py312/bin/python -m pytest exordos_s3/tests/integration/test_replication_rustfs.py
```

Set `EXORDOS_TEST_VICTORIAMETRICS_BINARY=/path/to/victoria-metrics-prod` as well
to verify real OTLP export and every Replication metrics panel query against a
disposable local metrics store. The error log panel uses VictoriaLogs and needs
separate validation. The fixture stops the RustFS and VictoriaMetrics processes
after the test.

The migration, ORM CRUD and concurrent-priority test uses a unique disposable
schema in a PostgreSQL database (the URL must allow creating schemas):

```sh
EXORDOS_TEST_POSTGRES_URL=postgresql://user@localhost/test_database \
  .tox/py312/bin/python -m pytest exordos_s3/tests/integration/test_replication_database.py
```
