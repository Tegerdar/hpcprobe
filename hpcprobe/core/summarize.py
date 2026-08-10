"""Aggregation layer behind the global `-t/--total` flag.

Collapses the detailed per-GPU / per-port / per-mount cluster state produced by
the command modules into two levels:

  * per-node   - one row per node, inner dimension averaged or summed
  * cluster    - one row for the whole cluster

Pure computation, no printing: the shape returned here is what
formatters/summary_out.py, csv_out.render_summary() and
prometheus_out.render_summary() all consume.

Returned shape:
    {
      "nodes":   {<node>: {<metric>: <number>, ...}, ...},
      "cluster": {<metric>: <number>, ...},
      "meta":    {"module": str, "nodes_total": int, "nodes_ok": int,
                  "nodes_error": int, "errors": {<node>: <reason>}},
    }
"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

# gpu.py emits module="gpus" and mem.py emits module="memory"; normalise both
# to the subcommand name so callers only ever deal with one spelling.
MODULE_ALIASES = {
    "gpus": "gpu",
    "memory": "mem",
    "network": "net",
    "storage": "stg",
}

# Filesystems whose capacity is served remotely: the same physical filesystem
# appears once per client node, so summing it across nodes would multiply the
# cluster's real capacity by the node count. These are counted once per
# (fstype, source, size); everything else is treated as node-local and summed.
NETWORK_FSTYPES = {
    "nfs", "nfs4", "cifs", "smb3", "beegfs", "fuse.beegfs", "lustre",
    "fuse.lustre", "gpfs", "ceph", "fuse.ceph", "glusterfs",
    "fuse.glusterfs", "9p", "afs", "panfs",
}

GB = 1024 * 1024 * 1024


def normalise_module(module: str) -> str:
    return MODULE_ALIASES.get(module, module)


def _f(val: Any, default: float = 0.0) -> float:
    """Tolerant float conversion - Slurm and df hand back strings."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _i(val: Any, default: int = 0) -> int:
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def _avg(values: Iterable[float]) -> float:
    vals = list(values)
    return sum(vals) / len(vals) if vals else 0.0


def _pct(part: float, whole: float) -> float:
    return (part / whole * 100.0) if whole else 0.0


def _round(d: Dict[str, float], ndigits: int = 1) -> Dict[str, Any]:
    """Round floats for display/JSON; leave ints alone so counts stay ints."""
    return {
        k: (round(v, ndigits) if isinstance(v, float) else v)
        for k, v in d.items()
    }


# --------------------------------------------------------------------------
# gpu
# --------------------------------------------------------------------------

def _summarize_gpu(state: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, Dict], Dict]:
    nodes: Dict[str, Dict[str, Any]] = {}
    all_utils: List[float] = []
    all_temps: List[float] = []
    c = {"gpus": 0, "power_w": 0.0, "mem_used_gb": 0.0, "mem_total_gb": 0.0}

    for node, nd in state.items():
        if "error" in nd:
            continue
        gpus = nd.get("gpus", [])
        if not gpus:
            nodes[node] = _round({
                "gpus": 0, "util_avg": 0.0, "util_max": 0.0, "temp_max": 0.0,
                "power_w": 0.0, "mem_used_gb": 0.0, "mem_total_gb": 0.0,
                "mem_used_pct": 0.0,
            })
            continue

        utils = [_f(g.get("util_pct")) for g in gpus]
        temps = [_f(g.get("temp_c")) for g in gpus]
        power = sum(_f(g.get("power_w")) for g in gpus)
        used_gb = sum(_f(g.get("mem_used_mb")) for g in gpus) / 1024.0
        total_gb = sum(_f(g.get("mem_total_mb")) for g in gpus) / 1024.0

        nodes[node] = _round({
            "gpus": len(gpus),
            "util_avg": _avg(utils),
            "util_max": max(utils),
            "temp_max": max(temps),
            "power_w": power,
            "mem_used_gb": used_gb,
            "mem_total_gb": total_gb,
            "mem_used_pct": _pct(used_gb, total_gb),
        })

        all_utils.extend(utils)
        all_temps.extend(temps)
        c["gpus"] += len(gpus)
        c["power_w"] += power
        c["mem_used_gb"] += used_gb
        c["mem_total_gb"] += total_gb

    cluster = _round({
        "gpus": c["gpus"],
        # Averaged over every GPU, not over nodes, so heterogeneous nodes
        # (4 GPUs vs 8 GPUs) don't get equal weight in the cluster figure.
        "util_avg": _avg(all_utils),
        "util_max": max(all_utils) if all_utils else 0.0,
        "temp_max": max(all_temps) if all_temps else 0.0,
        "power_w": c["power_w"],
        "mem_used_gb": c["mem_used_gb"],
        "mem_total_gb": c["mem_total_gb"],
        "mem_used_pct": _pct(c["mem_used_gb"], c["mem_total_gb"]),
    })
    return nodes, cluster


# --------------------------------------------------------------------------
# cpu
# --------------------------------------------------------------------------

def _summarize_cpu(state: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, Dict], Dict]:
    nodes: Dict[str, Dict[str, Any]] = {}
    loads: List[float] = []
    c = {"cpus_total": 0, "cpus_alloc": 0, "cpus_idle": 0, "sockets": 0}

    for node, nd in state.items():
        # cpu.py merges SSH data and Slurm data, so a node can have an SSH
        # error but still carry usable Slurm counters. Only skip when both
        # sides failed (matches print_console's own error test).
        if "error" in nd and "slurm_error" in nd:
            continue

        total = _i(nd.get("slurm_cputot", nd.get("cpu(s)")))
        alloc = _i(nd.get("slurm_alloccpus"))
        idle = _i(nd.get("slurm_idlecpus"))
        load = _f(nd.get("slurm_cpuload"))
        sockets = _i(nd.get("socket(s)"))

        nodes[node] = _round({
            "cpus_total": total,
            "cpus_alloc": alloc,
            "cpus_idle": idle,
            "alloc_pct": _pct(alloc, total),
            "load": load,
            "sockets": sockets,
        })

        loads.append(load)
        c["cpus_total"] += total
        c["cpus_alloc"] += alloc
        c["cpus_idle"] += idle
        c["sockets"] += sockets

    cluster = _round({
        "cpus_total": c["cpus_total"],
        "cpus_alloc": c["cpus_alloc"],
        "cpus_idle": c["cpus_idle"],
        "alloc_pct": _pct(c["cpus_alloc"], c["cpus_total"]),
        "load": _avg(loads),  # mean per-node load, not a meaningless sum
        "sockets": c["sockets"],
    })
    return nodes, cluster


# --------------------------------------------------------------------------
# mem
# --------------------------------------------------------------------------

def _summarize_mem(state: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, Dict], Dict]:
    nodes: Dict[str, Dict[str, Any]] = {}
    c = {k: 0.0 for k in
         ("os_total_gb", "os_avail_gb", "slurm_real_gb", "slurm_alloc_gb", "slurm_free_gb")}

    for node, nd in state.items():
        if "error" in nd and "slurm_error" in nd:
            continue

        os_total = _f(nd.get("os_memtotal_mb")) / 1024.0
        os_avail = _f(nd.get("os_memavailable_mb")) / 1024.0
        real = _f(nd.get("slurm_realmemory")) / 1024.0
        alloc = _f(nd.get("slurm_allocmem")) / 1024.0
        free = _f(nd.get("slurm_freemem")) / 1024.0

        nodes[node] = _round({
            "os_total_gb": os_total,
            "os_avail_gb": os_avail,
            "os_used_pct": _pct(os_total - os_avail, os_total),
            "slurm_real_gb": real,
            "slurm_alloc_gb": alloc,
            "slurm_free_gb": free,
            "slurm_alloc_pct": _pct(alloc, real),
        })

        c["os_total_gb"] += os_total
        c["os_avail_gb"] += os_avail
        c["slurm_real_gb"] += real
        c["slurm_alloc_gb"] += alloc
        c["slurm_free_gb"] += free

    cluster = _round({
        "os_total_gb": c["os_total_gb"],
        "os_avail_gb": c["os_avail_gb"],
        "os_used_pct": _pct(c["os_total_gb"] - c["os_avail_gb"], c["os_total_gb"]),
        "slurm_real_gb": c["slurm_real_gb"],
        "slurm_alloc_gb": c["slurm_alloc_gb"],
        "slurm_free_gb": c["slurm_free_gb"],
        "slurm_alloc_pct": _pct(c["slurm_alloc_gb"], c["slurm_real_gb"]),
    })
    return nodes, cluster


# --------------------------------------------------------------------------
# net
# --------------------------------------------------------------------------

_NET_ERROR_KEYS = (
    "rx_crc_errors_phy", "rx_symbol_err_phy", "rx_discards_phy",
    "tx_discards_phy", "link_down_events_phy",
)


def _summarize_net(state: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, Dict], Dict]:
    nodes: Dict[str, Dict[str, Any]] = {}
    c = {"ports": 0, "ports_up": 0, "ports_down": 0, "out_of_buffer": 0,
         "errors": 0, "link_down_events": 0, "pause_active": 0,
         "nodes_degraded": 0}

    for node, nd in state.items():
        if "error" in nd:
            continue

        netdevs = nd.get("netdevs", {})
        ports = nd.get("ports", [])
        up = sum(1 for p in ports if p.get("state") == "ACTIVE")
        oob = errs = downs = 0
        pause = False
        # One netdev can back several IB ports; count its counters once.
        seen_netdevs = set()

        for p in ports:
            nd_name = p.get("netdev", "-")
            if nd_name == "-" or nd_name in seen_netdevs:
                continue
            seen_netdevs.add(nd_name)
            stats = netdevs.get(nd_name, {}).get("stats", {})
            if not stats:
                continue
            oob += _i(stats.get("rx_out_of_buffer"))
            errs += sum(_i(stats.get(k)) for k in _NET_ERROR_KEYS)
            downs += _i(stats.get("link_down_events_phy"))
            if _i(stats.get("rx_pause_ctrl_phy")) or _i(stats.get("tx_pause_ctrl_phy")):
                pause = True

        down = len(ports) - up
        nodes[node] = {
            "ports": len(ports),
            "ports_up": up,
            "ports_down": down,
            "out_of_buffer": oob,
            "errors": errs,
            "link_down_events": downs,
            "pause_active": 1 if pause else 0,
        }

        c["ports"] += len(ports)
        c["ports_up"] += up
        c["ports_down"] += down
        c["out_of_buffer"] += oob
        c["errors"] += errs
        c["link_down_events"] += downs
        c["pause_active"] = max(c["pause_active"], 1 if pause else 0)
        if down or errs:
            c["nodes_degraded"] += 1

    cluster = dict(c)
    return nodes, cluster


# --------------------------------------------------------------------------
# stg
# --------------------------------------------------------------------------

def _mount_key(node: str, m: Dict[str, Any]) -> Tuple:
    """Identity used to avoid counting the same capacity twice.

    A shared filesystem (BeeGFS, Lustre, NFS...) is the same physical capacity
    seen from every client, so it is keyed without the node name and counted
    once cluster-wide. Node-local disks are keyed with the node name and
    summed normally.
    """
    fstype = m.get("fstype", "")
    if fstype in NETWORK_FSTYPES:
        return ("shared", fstype, m.get("source"), m.get("blocks_1k"))
    return ("local", node, m.get("mountpoint"), m.get("blocks_1k"))


def _blocks_gb(val: Any) -> float:
    return _f(val) / (1024.0 * 1024.0)


def _summarize_stg(state: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, Dict], Dict]:
    nodes: Dict[str, Dict[str, Any]] = {}

    cluster_seen: set = set()
    cluster_shared = 0
    c = {"mounts": 0, "size_gb": 0.0, "used_gb": 0.0, "avail_gb": 0.0}
    c_worst = -1.0
    beegfs_seen: set = set()
    beegfs_free_min: Optional[float] = None
    lustre_seen: set = set()
    lustre_use_max = -1.0

    for node, nd in state.items():
        if "error" in nd:
            continue

        node_seen: set = set()
        size = used = avail = 0.0
        worst = -1.0
        worst_mount = "-"

        for m in nd.get("mounts", []):
            key = _mount_key(node, m)
            if key in node_seen:
                continue
            node_seen.add(key)

            size += _blocks_gb(m.get("blocks_1k"))
            used += _blocks_gb(m.get("used_1k"))
            avail += _blocks_gb(m.get("avail_1k"))
            pct = _f(str(m.get("pcent", "")).rstrip("%"), -1.0)
            if pct > worst:
                worst, worst_mount = pct, m.get("mountpoint", "-")

            if key not in cluster_seen:
                cluster_seen.add(key)
                c["mounts"] += 1
                c["size_gb"] += _blocks_gb(m.get("blocks_1k"))
                c["used_gb"] += _blocks_gb(m.get("used_1k"))
                c["avail_gb"] += _blocks_gb(m.get("avail_1k"))
                if pct > c_worst:
                    c_worst = pct
            elif key[0] == "shared":
                cluster_shared += 1

        beegfs = nd.get("beegfs", {})
        bee_rows = list(beegfs.get("meta", [])) + list(beegfs.get("storage", []))
        bee_targets = [r for r in bee_rows if "target_id" in r]
        node_bee_min = min((_f(r.get("free_pct")) for r in bee_targets), default=None)
        for r in bee_targets:
            # BeeGFS targets are cluster-global; every client reports all of
            # them, so key on target id + pool rather than on the node.
            tkey = (r.get("target_id"), r.get("pool"))
            if tkey in beegfs_seen:
                continue
            beegfs_seen.add(tkey)
            fp = _f(r.get("free_pct"))
            beegfs_free_min = fp if beegfs_free_min is None else min(beegfs_free_min, fp)

        lustre_rows = [r for r in nd.get("lustre", [])
                       if "target" in r and not r.get("is_summary")]
        node_lustre_max = max((_f(r.get("use_pct")) for r in lustre_rows), default=-1.0)
        for r in lustre_rows:
            tkey = r.get("target")
            if tkey in lustre_seen:
                continue
            lustre_seen.add(tkey)
            lustre_use_max = max(lustre_use_max, _f(r.get("use_pct")))

        nodes[node] = _round({
            "mounts": len(node_seen),
            "size_gb": size,
            "used_gb": used,
            "avail_gb": avail,
            "use_pct": _pct(used, size),
            "worst_pct": worst,
            "worst_mount": worst_mount,
            "beegfs_targets": len(bee_targets),
            "beegfs_free_pct_min": node_bee_min if node_bee_min is not None else -1.0,
            "lustre_targets": len(lustre_rows),
            "lustre_use_pct_max": node_lustre_max,
        })

    cluster = _round({
        "mounts": c["mounts"],
        "size_gb": c["size_gb"],
        "used_gb": c["used_gb"],
        "avail_gb": c["avail_gb"],
        "use_pct": _pct(c["used_gb"], c["size_gb"]),
        "worst_pct": c_worst,
        "worst_mount": "-",
        "beegfs_targets": len(beegfs_seen),
        "beegfs_free_pct_min": beegfs_free_min if beegfs_free_min is not None else -1.0,
        "lustre_targets": len(lustre_seen),
        "lustre_use_pct_max": lustre_use_max,
        "shared_mounts_deduped": cluster_shared,
    })
    return nodes, cluster


_DISPATCH = {
    "gpu": _summarize_gpu,
    "cpu": _summarize_cpu,
    "mem": _summarize_mem,
    "net": _summarize_net,
    "stg": _summarize_stg,
}


def summarize(state: Dict[str, Dict[str, Any]], module: str) -> Dict[str, Any]:
    """Collapse a detailed cluster state into per-node and cluster rows."""
    module = normalise_module(module)
    fn = _DISPATCH.get(module)
    if fn is None:
        raise ValueError(f"no summary defined for module {module!r}")

    nodes, cluster = fn(state)

    # A node that dropped out is not the same as a node reporting zero, so
    # keep the failures visible rather than silently shrinking the denominator.
    errors = {n: d["error"] for n, d in state.items()
              if "error" in d and n not in nodes}

    return {
        "nodes": nodes,
        "cluster": cluster,
        "meta": {
            "module": module,
            "nodes_total": len(state),
            "nodes_ok": len(nodes),
            "nodes_error": len(errors),
            "errors": errors,
        },
    }
