# hpcprobe

HPC Administration & Telemetry Tool

A lightweight Python CLI for querying hardware metrics in Slurm-managed HPC clusters.

## Features

- **GPU Telemetry**: Real-time GPU metrics (utilization, memory, temperature, power) gathered via SSH and nvidia-smi on GPU nodes
- **CPU State**: OS-level CPU information (via lscpu) plus Slurm per-node CPU allocation and load
- **Memory Telemetry**: OS memory details (from /proc/meminfo) and Slurm memory allocation/state
- **Network Telemetry** (`net`): InfiniBand/RoCE port state and NIC error counters (via sysfs and ethtool), with optional delta mode to track counter drift between runs
- **Storage Telemetry** (`stg`): Filesystem usage (via df) plus BeeGFS/Lustre target-level capacity detail when those client tools are present
- **Job Queue** (`jobs`): Running and pending (idle) job counts straight from the scheduler - no SSH fan-out
- **Aggregate View**: `-t` collapses the detail rows to one row per node plus a single cluster-wide row, in every output format
- **Flexible Output**: Human-readable tables (default), JSON, CSV, or Prometheus OpenMetrics formats
- **Parallel Discovery**: Efficient batch polling across nodes with configurable concurrency

## Installation

```bash
pip install hpcprobe
```

The package installs a `hpcprobe` console script (entry point: `hpcprobe.cli:main`).

## Usage

General form:
```bash
hpcprobe <subcommand> [OPTIONS]
```

Global options are available for all subcommands (except `jobs`, which is cluster-wide and has no node dimension - see below):
- `-n [NODE ...]`       Which nodes to target - see below, this flag has three distinct states
- `-t`, `--total`       Aggregate view: one row per node plus a cluster total row
- `-j`, `--json`        Output in machine-readable JSON
- `-c`, `--csv`         Output in flattened CSV format
- `-p`, `--prometheus`  Output in Prometheus OpenMetrics format

### Targeting nodes (`-n`)

`-n` has three states, not two:

| Form | Effect |
|---|---|
| flag omitted | Discover nodes via `sinfo` (the default) |
| `-n node01 node02` | Target exactly those nodes, skipping discovery |
| `-n` with no names | Target only the host `hpcprobe` is running on - no SSH, no discovery |

The bare form exists for accounts that can reach the local host directly but
have no SSH access anywhere else - see [Running on a monitored node itself](#running-on-a-monitored-node-itself-no-ssh)
below. For example, a service account with a `/sbin/nologin` shell can do:
```bash
hpcprobe gpu -n -t -p
```
and get real data back for the local node with zero SSH involved, even
though that same account couldn't run `hpcprobe gpu` (full cluster) at all.

**Keep `-n` separated from other short flags** - `-n -t -p`, not `-ntp`.
Because `-n` can take a variable number of following names, argparse reads
`-ntp` as `-n` given the single node name `tp`, silently dropping `-t` and
`-p` rather than applying them. This isn't an hpcprobe quirk to be "fixed" -
it's how argparse's short-flag bundling works for any option with variable
arity, so it's simplest to just always write `-n` on its own.

Examples

### GPU Metrics
Query GPU status across all GPU nodes discovered via Slurm:
```bash
hpcprobe gpu
```

Probe a specific list of nodes (overrides Slurm discovery):
```bash
hpcprobe gpu -n node01 node02 node03
```

### CPU State
Show CPU hardware details and Slurm CPU allocation:
```bash
hpcprobe cpu
```

Include exhaustive CPU parameters:
```bash
hpcprobe cpu -e
```

Probe specific nodes:
```bash
hpcprobe cpu -n node01 node02
```

### Memory Telemetry
Show OS and Slurm memory metrics:
```bash
hpcprobe mem
```

Include every `/proc/meminfo` field rather than the common ones:
```bash
hpcprobe mem -e
```

Probe specific nodes:
```bash
hpcprobe mem -n node01 node02
```

### Network Telemetry
Show InfiniBand/RoCE port state and key NIC error counters (link errors, buffer drops, pause frames):
```bash
hpcprobe net
```

Include full per-netdev counters and interface details:
```bash
hpcprobe net -e
```

Show counter change since the last `net` run (snapshot-based; the first run establishes the baseline, subsequent runs report the delta and flag anything that moved):
```bash
hpcprobe net -d
```

Probe specific nodes:
```bash
hpcprobe net -n node01 node02
```

### Storage Telemetry
Show filesystem usage across all mounts, plus BeeGFS/Lustre target-level free space when `beegfs-df` or `lfs` are available on the node:
```bash
hpcprobe stg
```

Include small/system mounts and full per-target detail (by default, mounts under 3G and uniform-looking Lustre OST groups are collapsed for readability):
```bash
hpcprobe stg -e
```

Probe specific nodes:
```bash
hpcprobe stg -n node01 node02
```

### Job Queue
Show how many jobs are running and how many are pending (idle):
```bash
hpcprobe jobs
```

Count individual array tasks rather than one record per job array:
```bash
hpcprobe jobs --expand-arrays
```

This is the only subcommand that does not touch a compute node: it asks the
scheduler directly, so there is no SSH fan-out and no `sinfo` discovery step.

Note on terminology: Slurm's `PENDING` covers both genuinely eligible jobs and
blocked ones (dependency, held, QOS or association limits). It is the
equivalent of Moab's Idle **plus** Blocked, not Idle alone.

### Aggregate View (`-t`)
Collapse the inner dimension - GPUs, ports, mounts - into one row per node,
followed by a single cluster-wide row:
```bash
hpcprobe gpu -t
hpcprobe stg -t
hpcprobe net -t
```

`-t` applies to every output format, not just the table, so a scraper
configured with `-t -p` sees aggregate series only:
```bash
hpcprobe gpu -t -p     # hpcprobe_gpu_node_*{node="..."} and hpcprobe_gpu_cluster_*
hpcprobe stg -t -j     # {"nodes": {...}, "cluster": {...}, "meta": {...}}
```

How the aggregation is defined:

| Module | Per node | Cluster row |
|---|---|---|
| `gpu` | mean/max utilisation and temperature across the node's GPUs, summed power and VRAM | utilisation averaged **per GPU**, so an 8-GPU node outweighs a 2-GPU one |
| `cpu` | Slurm CPU total/alloc/idle, load | counts summed, load is the **mean** of per-node loads |
| `mem` | OS and Slurm memory, allocation percentage | summed, percentages recomputed from the totals |
| `net` | port counts, summed error counters per node | summed, plus a count of degraded nodes |
| `stg` | capacity summed over distinct filesystems, worst mount percentage | shared filesystems (BeeGFS, Lustre, NFS, GPFS, Ceph...) are counted **once**, node-local disks are summed |

That last row matters: a 500 TB BeeGFS mounted on 200 clients would otherwise
report 100 PB of cluster capacity. Mounts are keyed by `(fstype, source, size)`
for shared filesystems and by `(node, mountpoint, size)` for local ones. The
footer reports how many mounts were folded together.

Unreachable nodes are not silently dropped from the aggregate: the footer and
the `hpcprobe_summary_nodes_{total,ok,error}` metrics carry the reachability count
so a node that stops answering does not just quietly disappear from the series.

Where a value is unknown, percentage fields use `-1` as a sentinel (rendered as
`-` in the table). Guard for it in alert expressions.

### Output Format Examples
Append a format flag to any command:
```bash
hpcprobe gpu -j        # JSON
hpcprobe gpu -c        # CSV
hpcprobe gpu -p        # Prometheus (OpenMetrics)
hpcprobe cpu -e -j     # Extended CPU info in JSON
hpcprobe net -d -j     # Network delta, JSON
hpcprobe gpu -t -p     # Per-node and cluster aggregates, Prometheus
hpcprobe jobs -p       # Queue depth, Prometheus
```

`-t` and `-e` are mutually redundant: `-t` replaces the detail rows entirely,
so `-e` has no effect alongside it.

Note: Prometheus output is emitted in text exposition format suitable for scraping or pushing into a textfile collector.

## Requirements

- Python >= 3.9
- `ssh` (for remote polling on cluster nodes)
- Slurm utilities available on the execution host:
  - `sinfo` (used for node discovery)
  - `scontrol` (used to query per-node Slurm state)
  - `squeue` (used by the `jobs` subcommand)
- On target nodes:
  - `nvidia-smi` for GPU telemetry (if GPUs are present)
  - `lscpu` for CPU hardware queries (for the `cpu` subcommand)
  - `ethtool` for NIC error counters (for the `net` subcommand); sysfs (`/sys/class/infiniband`, `/sys/class/net`) is used for link state and requires no additional tooling
  - `df` for filesystem usage (for the `stg` subcommand); `beegfs-df` and/or `lfs` are used automatically if installed, but are not required
- Network reachability / SSH key setup so the execution host can SSH into compute nodes (BatchMode=no/keys)

All remote queries are read-only and do not require root or sudo on target nodes.

If Slurm discovery fails or required utilities are missing, hpcprobe will print an error and fall back to user-supplied node lists when possible.

### Running on a monitored node itself (no SSH)

If a target node is the same host hpcprobe is running on, hpcprobe skips SSH entirely and runs the query directly. This matters for service accounts that either have no SSH key set up, or whose login shell is intentionally something like `/sbin/nologin` (common for accounts such as `zabbix`, which should not otherwise be able to SSH anywhere) - such an account can still monitor the local machine it's already running on without any SSH access at all. SSH is still used for every genuinely remote node.

This is what the bare `-n` form (see [Targeting nodes](#targeting-nodes--n) above) is for in practice - `hpcprobe gpu -n -t -p` never resolves to anything but the local host, so it never needs SSH. Explicitly naming the local host (`hpcprobe gpu -n node01`) gets the same result, since node identity for the SSH short-circuit is a separate check: the account's own hostname (`gethostname()`/`getfqdn()`, short and FQDN forms, case-insensitive) matched against whatever node name was targeted, however it was chosen.

If you need to force SSH even for the local host - e.g. to test SSH trust independently of this shortcut - set `HPCPROBE_FORCE_SSH=1`:

```bash
HPCPROBE_FORCE_SSH=1 hpcprobe gpu -n node01
```

## Delta snapshots (net -d)

`hpcprobe net -d` saves a snapshot after each run to `$XDG_CACHE_HOME/hpcprobe/net_snapshot.json` (or `~/.cache/hpcprobe/` if `XDG_CACHE_HOME` is unset). Each run compares against the previous snapshot on the same execution host; there is no shared state across different hosts or users. Counter resets (e.g. after a driver reload) are reported as `reset` rather than a misleading negative delta.

Do not combine `-d` with an unattended scraper: it mutates the snapshot on
every run, so two schedules pointed at the same cache will interfere with each
other's baselines.

## Exit codes

- `0` — success
- non-zero — failure or no targets identified (examples: 1 for no targets, 130 for keyboard interrupt)

## Contributing

Bug reports, feature requests, and patches are welcome via GitHub issues and pull requests.

## License

MIT
