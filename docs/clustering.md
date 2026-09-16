# Distributed S3 instances

A `distributed` instance is one RustFS cluster over 4 to 16 nodes, each with a
single data disk. Every node serves the whole S3 API and stores objects
erasure-coded across the cluster, so an object written through one node is read
through any other.

The Russian translation of this page is [clustering.ru.md](clustering.ru.md).

## Creating one

```json
{
  "name": "my-s3-cluster",
  "kind": "distributed",
  "nodes_number": 4,
  "parity": 1,
  "cpu": 2,
  "ram": 4096,
  "disk_size": 100,
  "version": "/v1/types/s3/versions/<uuid>"
}
```

`parity` is the erasure-coding parity RustFS writes with
(`RUSTFS_STORAGE_CLASS_STANDARD=EC:<parity>`); it can be at most half of
`nodes_number`. Left out, RustFS picks its own default for the set width: 2 for
4–5 nodes, 3 for 6–7, 4 for 8 and more. Parity trades capacity for how many
nodes the cluster survives:

| Nodes | Parity | Usable capacity | Reads survive | Writes survive |
|---|---|---|---|---|
| 4 | 2 (default) | 50% | 2 nodes | 1 node |
| 4 | 1 | 75% | 1 node | 1 node |
| 6 | 3 (default) | 50% | 3 nodes | 2 nodes |
| 8 | 2 | 75% | 2 nodes | 2 nodes |
| 16 | 4 (default) | 75% | 4 nodes | 4 nodes |

Parity is recorded in the metadata of every object as it is written, so a
change would only apply to new objects. That is one of the reasons the layout
is immutable (below).

## The layout is fixed at creation

`kind`, `nodes_number` and `parity` are accepted on create and refused on
update, and so are `cpu` and `ram` of a distributed instance. `disk_size` can
grow; it never shrinks.

- RustFS cannot change the drive count of a pool: the width of an erasure set
  is written into the on-disk format, and a node set that lost a node would
  lose its drives with it.
- Changing `cpu` or `ram` recreates every VM of the set at once, which takes
  the whole cluster down.

Growing a cluster means creating a second instance and moving the data through
S3.

## Endpoints and membership

Nodes address each other by name, not by IP:

```
RUSTFS_VOLUMES=http://node{1...4}.rustfs.internal:9000/var/lib/rustfs/data
```

The names resolve through a block in `/etc/hosts` that
`/usr/local/bin/exordos-s3-sync-hosts` rewrites from `EXORDOS_S3_HOSTS` in
`rustfs.env` before RustFS starts. RustFS identifies a pool by the literal
endpoint string, so a name outlives a change of address, and a single ellipsis
pattern is the only form to which further pools could ever be appended.

Ordinals are frozen once, when every node of the set has an address, and name
both a hostname and a drive position in the erasure set. A node that
disappears from the set, or a node the set gained that the membership does not
know, puts the instance into `ERROR`: the nodes it still has keep serving, but
a replacement node cannot take a free ordinal on its own. The instance stays in
`ERROR` whatever the nodes report. `members` is read-only through the API, so
there is no recovery in place: copy the data out through the nodes that still
serve, then recreate the instance.

## One node applies the shared state

Buckets, users, policies and access keys live in the cluster itself, so
applying them from several nodes at once would let a node holding a stale
target delete what another has just created. The control plane marks the node
with ordinal 1 as the reconciler; the others only report what they see. While
that node is down, API changes to buckets and users wait, and data keeps being
served by the rest.

## What the status means

The status of a distributed instance follows RustFS, not the VMs. Each node
reads its own `/health/ready` and reports it; the instance turns `ACTIVE` once
every node says it serves — a minute or two after the node set is up, which is
how long the nodes take to find each other and load IAM.

A node whose RustFS stops serving reports nothing at all — it cannot read the
state it is supposed to report — so the instance keeps the verdict it had
rather than reading as "coming up" again. In other words `ACTIVE` means every
node reported that it serves, and none has reported otherwise since; it does
not mean every node is serving right now. A node set that is not `ACTIVE` —
a VM down, a disk being grown, nodes being re-imaged — gives the instance its
own status, and membership drift makes it `ERROR`.

## Operating notes

- **Upgrading the element re-images every node.** The image URL lives in the
  version record, and each node is re-imaged as the instance is actualized.
  Data survives on the separate data disk, except for the most recent writes:
  the node is destroyed rather than shut down, and RustFS does not fsync a
  PUT, so an object written in the last seconds before that is lost. Measured
  on the stand: an object written at the moment of the destroy was gone
  afterwards, one written a minute earlier survived.
- **Growing the data disk keeps the cluster serving.** The disk and its
  filesystem grow in place, no node reboots, and the instance stays `ACTIVE`.
- **RustFS ignores SIGTERM while it waits for its peers at start.** The systemd
  unit therefore lets systemd send SIGKILL after `TimeoutStopSec`; without it a
  restart in that window leaves the process behind and the unit never starts
  again.
- **Placement is not guaranteed.** The core only applies soft anti-affinity, so
  the nodes of a cluster can end up on one hypervisor, and losing it takes the
  cluster with it.

## Not supported

Adding or removing nodes, pools, rolling restarts and reassigning the
reconciler.
