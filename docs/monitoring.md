# Monitoring S3 instances

The nodes of an S3 instance send their metrics to the platform
VictoriaMetrics of the `observability` element. Nothing is sent until that
element is deployed: the vmagent of the base image waits for
`victoria-storage.local.genesis-core.tech` to resolve before it starts.

RustFS metrics need a node image built on `exordos_base` 1.3.1 or later, whose
vmagent accepts OTLP on `127.0.0.1:8430`. A node on an older image still gets
the new `rustfs.env` and restarts RustFS, but its metrics are dropped without a
log line; they arrive once the instance moves to a version with a newer image.

## What is collected

| Source | Path | Labels that name the instance |
|---|---|---|
| node_exporter of the base image | scraped by vmagent every 15 s | `instance` — the node host name, `s3aas-dp-<instance uuid>-node-<suffix>` |
| RustFS | OTLP push to the local vmagent every 30 s | `exordos_s3_instance`, `exordos_project` |

node_exporter covers the node itself, including the data disk mounted at
`/var/lib/rustfs/data`. RustFS adds its own view: cluster capacity, drives,
requests, objects and buckets (`rustfs_cluster_*`, `rustfs_system_drive_*`,
`rustfs_node_disk_*` and more).

RustFS labels its metrics with the attributes the control plane writes into
`rustfs.env` (`OTEL_RESOURCE_ATTRIBUTES`). Per node series also carry
`server`/`drive` (the RustFS endpoint of the drive) and `network.local.address`
(the node address), and RustFS adds `rustfs.cluster.id` (the instance uuid
again) and `collection_scope`: `local` for a node's own drives, `cluster` for
its view of the whole cluster. Every node of a distributed instance reports the
cluster-wide `rustfs_cluster_*` series, so take one of them rather than a sum.

## How full the disks are

The fullest drive is the one that stops writes: RustFS refuses a write once any
drive of the erasure set lacks room for its shard, or once less than 1% of the
pool is free. As `df` counts it, root-reserved blocks left out:

```promql
max by (exordos_s3_instance) (
  label_replace(
    100 * (
      node_filesystem_size_bytes{mountpoint="/var/lib/rustfs/data"}
      - node_filesystem_free_bytes{mountpoint="/var/lib/rustfs/data"}
    ) / (
      node_filesystem_size_bytes{mountpoint="/var/lib/rustfs/data"}
      - node_filesystem_free_bytes{mountpoint="/var/lib/rustfs/data"}
      + node_filesystem_avail_bytes{mountpoint="/var/lib/rustfs/data"}
    ),
    "exordos_s3_instance", "$1", "instance", "s3aas-dp-(.+)-node-.+"
  )
)
```

The same from RustFS, per drive:

```promql
max by (exordos_s3_instance) (
  100 * rustfs_system_drive_used_bytes / rustfs_system_drive_total_bytes
)
```

Space left for objects, after erasure coding, as one node sees the cluster:

```promql
max by (exordos_s3_instance) (rustfs_cluster_capacity_free_bytes)
```

The data disk also holds `/var/log`, bind-mounted from the same file system,
so the node's logs count against it.

## Dashboard

The `s3_dashboard` element puts an **S3 instance** dashboard into the **S3**
folder of the shared observability Grafana, per project and instance:

- capacity: disk fullness per node, space left for objects, raw and usable space;
- health: nodes and drives online, offline and healing;
- data safety: how many more drives can be lost, objects waiting for repair,
  write quorum failures, nodes offline, internode errors, free inodes;
- usage accounting: the last scanner cycle, when bucket usage was last saved
  and whether it converged, quota checks that failed. Quotas and object counts
  depend on it; after an upgrade from 1.0.0-beta.4 it shows `never`;
- buckets: size, objects and quota fill of each bucket;
- traffic and operations: requests by status and S3 operation, share of 5xx,
  mean latency, bytes sent;
- process: RustFS memory against node RAM, restarts and OOM kills.

The element depends on the `observability` element; install it after that one.

## Notes

- **The endpoint is the root one on purpose.** RustFS 1.0.0 only
  turns its stdout exporter off when
  `RUSTFS_OBS_ENDPOINT` is set; with just `RUSTFS_OBS_METRIC_ENDPOINT` it dumps
  every metric to stdout, and so to the journal. Traces and logs are switched
  off explicitly, since vmagent only accepts metrics, and
  `RUSTFS_OBS_LOG_STDOUT_ENABLED` keeps the logs in the journal.
- **Request latency is a mean.** RustFS 1.0.0 buckets
  `rustfs_http_server_request_duration_seconds` on millisecond bounds while it
  records seconds, so every request lands in the first bucket and quantiles
  mean nothing; divide `_sum` by `_count` instead.
- **Object and bucket counts arrive in bursts.** RustFS 1.0.0 publishes
  `rustfs_cluster_usage_*` and `rustfs_cluster_buckets_total` only once a usage
  snapshot has converged, so query them with `last_over_time`.
- **Without the observability element RustFS stays quiet.** The failed exports
  are dropped without a log line, and readiness is unaffected.
- **Changing the labels or the endpoint restarts RustFS** on every node at
  once, like any change of `rustfs.env`.
