from typing import Any, Dict, List, Tuple


def _to_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _lbl(val: Any) -> str:
    """Escape a label value per the Prometheus exposition format.

    A single unescaped backslash or quote (mountpoints and GPU model strings
    can contain both) makes the whole exposition unparseable, not just the one
    line, which takes every dependent item down with it.
    """
    return (
        str(val)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def render(data: Dict[str, Any], module: str) -> str:
    """Translates raw dictionary data into Prometheus exposition format."""
    lines: List[str] = []

    def add_metric(name: str, help_text: str, mtype: str, points: List[Tuple[str, float]]) -> None:
        if not points:
            return
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
        for labels, value in points:
            # `metric{} 1` parses under the spec but several consumers
            # (Zabbix's prometheus_pattern among them) will not match it
            # against a bare metric name, so drop the braces entirely.
            lines.append(f"{name}{{{labels}}} {value}" if labels else f"{name} {value}")

    # GPU section: per-GPU metrics, one label set per (node, gpu_index)
    if module in ("gpus", "gpu"):
        util, mem_used, mem_tot, temp, pwr, up = [], [], [], [], [], []

        for node, node_data in data.items():
            if "error" in node_data:
                continue
            for gpu in node_data.get("gpus", []):
                lbl = f'node="{_lbl(node)}",gpu_index="{_lbl(gpu["index"])}",model="{_lbl(gpu["model"])}"'
                # 1/0 rather than absence: an alert can fire on == 0, but
                # cannot fire on a series that stopped being exported.
                up.append((lbl, 1.0 if gpu.get("status", "ok") == "ok" else 0.0))
                for bucket, key, scale in (
                    (util, "util_pct", 1.0),
                    (mem_used, "mem_used_mb", 1048576.0),  # MB -> Bytes
                    (mem_tot, "mem_total_mb", 1048576.0),
                    (temp, "temp_c", 1.0),
                    (pwr, "power_w", 1.0),
                ):
                    value = gpu.get(key)
                    if value is not None:  # never export a placeholder as 0
                        bucket.append((lbl, value * scale))

        add_metric("hpcprobe_gpu_utilization_percent", "GPU Utilization %", "gauge", util)
        add_metric("hpcprobe_gpu_memory_used_bytes", "GPU Memory Used (Bytes)", "gauge", mem_used)
        add_metric("hpcprobe_gpu_memory_total_bytes", "GPU Memory Total (Bytes)", "gauge", mem_tot)
        add_metric("hpcprobe_gpu_temperature_celsius", "GPU Temperature (C)", "gauge", temp)
        add_metric("hpcprobe_gpu_power_draw_watts", "GPU Power Draw (W)", "gauge", pwr)
        add_metric("hpcprobe_gpu_up", "1 if every GPU metric was readable", "gauge", up)

    # CPU section: export per-node metrics (do not assume local hostname)
    elif module in ("cpu", "cpus"):
        cores: List[Tuple[str, float]] = []
        sockets: List[Tuple[str, float]] = []
        slurm_total: List[Tuple[str, float]] = []
        slurm_load: List[Tuple[str, float]] = []

        for node, node_data in data.items():
            if "error" in node_data:
                continue
            model = node_data.get("model_name", "unknown")
            lbl = f'node="{_lbl(node)}",model="{_lbl(model)}"'
            cores.append((lbl, _to_float(node_data.get("cpu(s)"))))
            sockets.append((lbl, _to_float(node_data.get("socket(s)"))))

            if "slurm_cputot" in node_data:
                slurm_total.append((lbl, _to_float(node_data.get("slurm_cputot"))))
            if "slurm_cpuload" in node_data:
                slurm_load.append((lbl, _to_float(node_data.get("slurm_cpuload"))))

        add_metric("hpcprobe_cpu_cores_total", "Total OS CPU Cores", "gauge", cores)
        add_metric("hpcprobe_cpu_sockets_total", "Total CPU Sockets", "gauge", sockets)
        add_metric("hpcprobe_slurm_cpu_total", "Total CPUs allocated to Slurm", "gauge", slurm_total)
        add_metric("hpcprobe_slurm_cpu_load", "Current Slurm CPU Load", "gauge", slurm_load)

    # Network section: per-port IB/RoCE link state + key ethtool error counters
    elif module in ("net", "network"):
        link_up: List[Tuple[str, float]] = []
        out_of_buffer: List[Tuple[str, float]] = []
        crc_errors: List[Tuple[str, float]] = []
        symbol_errors: List[Tuple[str, float]] = []
        rx_discards: List[Tuple[str, float]] = []
        tx_discards: List[Tuple[str, float]] = []
        link_down_events: List[Tuple[str, float]] = []
        pause_active: List[Tuple[str, float]] = []

        for node, node_data in data.items():
            if "error" in node_data:
                continue
            netdevs = node_data.get("netdevs", {})
            for p in node_data.get("ports", []):
                nd = p["netdev"]
                lbl = f'node="{_lbl(node)}",device="{_lbl(p["device"])}",netdev="{_lbl(nd)}",link_layer="{_lbl(p["link_layer"])}"'
                link_up.append((lbl, 1.0 if p["state"] == "ACTIVE" else 0.0))

                stats = netdevs.get(nd, {}).get("stats", {}) if nd != "-" else {}
                if not stats:
                    continue
                out_of_buffer.append((lbl, _to_float(stats.get("rx_out_of_buffer"))))
                crc_errors.append((lbl, _to_float(stats.get("rx_crc_errors_phy"))))
                symbol_errors.append((lbl, _to_float(stats.get("rx_symbol_err_phy"))))
                rx_discards.append((lbl, _to_float(stats.get("rx_discards_phy"))))
                tx_discards.append((lbl, _to_float(stats.get("tx_discards_phy"))))
                link_down_events.append((lbl, _to_float(stats.get("link_down_events_phy"))))
                pause_on = _to_float(stats.get("rx_pause_ctrl_phy")) > 0 or _to_float(stats.get("tx_pause_ctrl_phy")) > 0
                pause_active.append((lbl, 1.0 if pause_on else 0.0))

        add_metric("hpcprobe_ib_port_link_up", "1 if port state is ACTIVE, else 0", "gauge", link_up)
        add_metric("hpcprobe_net_rx_out_of_buffer_total", "RX ring buffer overflow count", "counter", out_of_buffer)
        add_metric("hpcprobe_net_rx_crc_errors_phy_total", "PHY-level RX CRC errors", "counter", crc_errors)
        add_metric("hpcprobe_net_rx_symbol_errors_phy_total", "PHY-level RX symbol errors", "counter", symbol_errors)
        add_metric("hpcprobe_net_rx_discards_phy_total", "PHY-level RX discards", "counter", rx_discards)
        add_metric("hpcprobe_net_tx_discards_phy_total", "PHY-level TX discards", "counter", tx_discards)
        add_metric("hpcprobe_net_link_down_events_phy_total", "PHY-level link down event count", "counter", link_down_events)
        add_metric("hpcprobe_net_pause_frames_active", "1 if RX or TX pause frames observed, else 0", "gauge", pause_active)

    # Storage section: mount-level usage plus BeeGFS/Lustre target free%
    elif module in ("stg", "storage"):
        mount_use_pct: List[Tuple[str, float]] = []
        mount_avail_bytes: List[Tuple[str, float]] = []
        beegfs_free_pct: List[Tuple[str, float]] = []
        lustre_use_pct: List[Tuple[str, float]] = []

        for node, node_data in data.items():
            if "error" in node_data:
                continue

            for m in node_data.get("mounts", []):
                lbl = f'node="{_lbl(node)}",mountpoint="{_lbl(m["mountpoint"])}",fstype="{_lbl(m["fstype"])}"'
                pcent = str(m.get("pcent", "")).rstrip("%")
                mount_use_pct.append((lbl, _to_float(pcent, -1.0)))
                try:
                    avail_bytes = int(m["avail_1k"]) * 1024.0
                    mount_avail_bytes.append((lbl, avail_bytes))
                except (ValueError, KeyError):
                    pass

            beegfs = node_data.get("beegfs", {})
            for kind, rows in (("meta", beegfs.get("meta", [])), ("storage", beegfs.get("storage", []))):
                for r in rows:
                    if "target_id" not in r:
                        continue
                    lbl = f'node="{_lbl(node)}",target_id="{_lbl(r["target_id"])}",kind="{kind}",pool="{_lbl(r["pool"])}"'
                    beegfs_free_pct.append((lbl, _to_float(r.get("free_pct"))))

            for r in node_data.get("lustre", []):
                if "target" not in r:
                    continue
                lbl = f'node="{_lbl(node)}",target="{_lbl(r["target"])}"'
                lustre_use_pct.append((lbl, _to_float(r.get("use_pct"))))

        add_metric("hpcprobe_storage_mount_use_percent", "Filesystem use percentage (df)", "gauge", mount_use_pct)
        add_metric("hpcprobe_storage_mount_available_bytes", "Filesystem available space (bytes)", "gauge", mount_avail_bytes)
        add_metric("hpcprobe_beegfs_target_free_percent", "BeeGFS target free space percentage", "gauge", beegfs_free_pct)
        add_metric("hpcprobe_lustre_target_use_percent", "Lustre target (MDT/OST) use percentage", "gauge", lustre_use_pct)

    # Memory section: export OS and Slurm memory metrics per-node (MB -> bytes)
    elif module in ("memory", "mem"):
        os_total: List[Tuple[str, float]] = []
        os_avail: List[Tuple[str, float]] = []
        os_free: List[Tuple[str, float]] = []
        buffers: List[Tuple[str, float]] = []
        cached: List[Tuple[str, float]] = []
        swap_tot: List[Tuple[str, float]] = []
        swap_free: List[Tuple[str, float]] = []
        slurm_real: List[Tuple[str, float]] = []
        slurm_alloc: List[Tuple[str, float]] = []
        slurm_free: List[Tuple[str, float]] = []

        def mb_to_bytes(v: Any) -> float:
            return _to_float(v) * 1024.0 * 1024.0

        for node, node_data in data.items():
            if "error" in node_data:
                continue
            lbl = f'node="{_lbl(node)}"'
            # keys populated by mem.poll_node: os_<Key>_mb
            os_total.append((lbl, mb_to_bytes(node_data.get("os_memtotal_mb"))))
            os_avail.append((lbl, mb_to_bytes(node_data.get("os_memavailable_mb"))))
            os_free.append((lbl, mb_to_bytes(node_data.get("os_memfree_mb"))))
            buffers.append((lbl, mb_to_bytes(node_data.get("os_buffers_mb"))))
            cached.append((lbl, mb_to_bytes(node_data.get("os_cached_mb"))))
            swap_tot.append((lbl, mb_to_bytes(node_data.get("os_swaptotal_mb"))))
            swap_free.append((lbl, mb_to_bytes(node_data.get("os_swapfree_mb"))))

            # Slurm keys are MB already: slurm_realmemory, slurm_allocmem, slurm_freemem
            slurm_real.append((lbl, mb_to_bytes(node_data.get("slurm_realmemory"))))
            slurm_alloc.append((lbl, mb_to_bytes(node_data.get("slurm_allocmem"))))
            slurm_free.append((lbl, mb_to_bytes(node_data.get("slurm_freemem"))))

        add_metric("hpcprobe_os_memory_total_bytes", "OS Memory Total (bytes)", "gauge", os_total)
        add_metric("hpcprobe_os_memory_available_bytes", "OS Memory Available (bytes)", "gauge", os_avail)
        add_metric("hpcprobe_os_memory_free_bytes", "OS Memory Free (bytes)", "gauge", os_free)
        add_metric("hpcprobe_os_memory_buffers_bytes", "OS Memory Buffers (bytes)", "gauge", buffers)
        add_metric("hpcprobe_os_memory_cached_bytes", "OS Memory Cached (bytes)", "gauge", cached)
        add_metric("hpcprobe_os_swap_total_bytes", "OS Swap Total (bytes)", "gauge", swap_tot)
        add_metric("hpcprobe_os_swap_free_bytes", "OS Swap Free (bytes)", "gauge", swap_free)
        add_metric("hpcprobe_slurm_memory_real_bytes", "Slurm RealMemory (bytes)", "gauge", slurm_real)
        add_metric("hpcprobe_slurm_memory_alloc_bytes", "Slurm AllocMem (bytes)", "gauge", slurm_alloc)
        add_metric("hpcprobe_slurm_memory_free_bytes", "Slurm FreeMem (bytes)", "gauge", slurm_free)

    # Jobs section: cluster-wide scheduler queue depth. Unlike every other
    # module the payload is flat (no per-node dimension), so there is no
    # node label - the whole point is one number per state.
    elif module == "jobs":
        by_state = [
            (f'state="{_lbl(state)}"', float(count))
            for state, count in sorted(data.get("states", {}).items())
        ]
        add_metric("hpcprobe_jobs_by_state", "Jobs per Slurm state", "gauge", by_state)
        add_metric("hpcprobe_jobs_running", "Jobs in RUNNING state", "gauge",
                   [("", _to_float(data.get("running")))])
        add_metric("hpcprobe_jobs_pending", "Jobs in PENDING state (idle)", "gauge",
                   [("", _to_float(data.get("pending")))])
        add_metric("hpcprobe_jobs_other", "Jobs in any other state", "gauge",
                   [("", _to_float(data.get("other")))])
        add_metric("hpcprobe_jobs_total", "Jobs known to the scheduler", "gauge",
                   [("", _to_float(data.get("total")))])

    return "\n".join(lines)


# --------------------------------------------------------------------------
# -t / --total
# --------------------------------------------------------------------------

_GB = 1024.0 * 1024.0 * 1024.0

# (summary_key, metric_suffix, help text, prometheus type, scale factor).
# Every entry is emitted twice: once per node as
# hpcprobe_<module>_node_<suffix>{node="..."} and once for the whole cluster as
# hpcprobe_<module>_cluster_<suffix> with no labels. Keys absent from a given
# level are skipped rather than defaulted, so nothing invents a zero.
SUMMARY_METRICS: Dict[str, List[Tuple[str, str, str, str, float]]] = {
    "gpu": [
        ("gpus", "gpus_total", "GPUs present", "gauge", 1.0),
        ("gpus_unreadable", "unreadable_total", "GPUs with unreadable metrics", "gauge", 1.0),
        ("util_avg", "utilization_percent_avg", "Mean GPU utilization", "gauge", 1.0),
        ("util_max", "utilization_percent_max", "Busiest GPU utilization", "gauge", 1.0),
        ("temp_max", "temperature_celsius_max", "Hottest GPU temperature", "gauge", 1.0),
        ("power_w", "power_draw_watts", "Summed GPU power draw", "gauge", 1.0),
        ("mem_used_gb", "memory_used_bytes", "Summed GPU memory used", "gauge", _GB),
        ("mem_total_gb", "memory_total_bytes", "Summed GPU memory installed", "gauge", _GB),
        ("mem_used_pct", "memory_used_percent", "GPU memory used percentage", "gauge", 1.0),
    ],
    "cpu": [
        ("cpus_total", "cpus_total", "CPUs known to Slurm", "gauge", 1.0),
        ("cpus_alloc", "cpus_allocated", "CPUs allocated by Slurm", "gauge", 1.0),
        ("cpus_idle", "cpus_idle", "CPUs idle in Slurm", "gauge", 1.0),
        ("alloc_pct", "cpus_allocated_percent", "CPU allocation percentage", "gauge", 1.0),
        ("load", "load", "CPU load (mean of per-node loads at cluster level)", "gauge", 1.0),
        ("sockets", "sockets_total", "CPU sockets", "gauge", 1.0),
    ],
    "mem": [
        ("os_total_gb", "os_memory_total_bytes", "OS memory installed", "gauge", _GB),
        ("os_avail_gb", "os_memory_available_bytes", "OS memory available", "gauge", _GB),
        ("os_used_pct", "os_memory_used_percent", "OS memory used percentage", "gauge", 1.0),
        ("slurm_real_gb", "slurm_memory_real_bytes", "Slurm RealMemory", "gauge", _GB),
        ("slurm_alloc_gb", "slurm_memory_alloc_bytes", "Slurm AllocMem", "gauge", _GB),
        ("slurm_free_gb", "slurm_memory_free_bytes", "Slurm FreeMem", "gauge", _GB),
        ("slurm_alloc_pct", "slurm_memory_alloc_percent", "Slurm memory allocation percentage", "gauge", 1.0),
    ],
    "net": [
        ("ports", "ports_total", "IB/RoCE ports", "gauge", 1.0),
        ("ports_up", "ports_up", "IB/RoCE ports in ACTIVE state", "gauge", 1.0),
        ("ports_down", "ports_down", "IB/RoCE ports not in ACTIVE state", "gauge", 1.0),
        ("out_of_buffer", "rx_out_of_buffer_total", "Summed RX ring buffer overflows", "counter", 1.0),
        ("errors", "errors_total", "Summed CRC, symbol, discard and link-down counters", "counter", 1.0),
        ("link_down_events", "link_down_events_total", "Summed PHY link down events", "counter", 1.0),
        ("pause_active", "pause_active", "1 if any port observed pause frames", "gauge", 1.0),
        ("nodes_degraded", "nodes_degraded", "Nodes with a down port or non-zero errors", "gauge", 1.0),
    ],
    "stg": [
        ("mounts", "mounts_total", "Distinct filesystems counted", "gauge", 1.0),
        ("size_gb", "size_bytes", "Summed filesystem capacity", "gauge", _GB),
        ("used_gb", "used_bytes", "Summed filesystem space used", "gauge", _GB),
        ("avail_gb", "available_bytes", "Summed filesystem space available", "gauge", _GB),
        ("use_pct", "use_percent", "Aggregate filesystem use percentage", "gauge", 1.0),
        ("worst_pct", "worst_mount_use_percent", "Highest single-mount use percentage", "gauge", 1.0),
        ("beegfs_targets", "beegfs_targets_total", "Distinct BeeGFS targets seen", "gauge", 1.0),
        ("beegfs_free_pct_min", "beegfs_free_percent_min", "Lowest BeeGFS target free percentage", "gauge", 1.0),
        ("lustre_targets", "lustre_targets_total", "Distinct Lustre targets seen", "gauge", 1.0),
        ("lustre_use_pct_max", "lustre_use_percent_max", "Highest Lustre target use percentage", "gauge", 1.0),
        ("shared_mounts_deduped", "shared_mounts_deduped", "Shared-filesystem mounts folded into one", "gauge", 1.0),
    ],
}


def render_summary(summary: Dict[str, Any], module: str) -> str:
    """Prometheus exposition for `-t`: per-node aggregates plus cluster totals."""
    lines: List[str] = []
    specs = SUMMARY_METRICS.get(module, [])
    nodes = summary.get("nodes", {})
    cluster = summary.get("cluster", {})
    meta = summary.get("meta", {})

    def add_metric(name: str, help_text: str, mtype: str, points: List[Tuple[str, float]]) -> None:
        if not points:
            return
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
        for labels, value in points:
            lines.append(f"{name}{{{labels}}} {value}" if labels else f"{name} {value}")

    for key, suffix, help_text, mtype, scale in specs:
        node_points = [
            (f'node="{_lbl(node)}"', _to_float(vals[key]) * scale)
            for node, vals in sorted(nodes.items())
            if key in vals
        ]
        add_metric(f"hpcprobe_{module}_node_{suffix}", help_text, mtype, node_points)

        if key in cluster:
            add_metric(
                f"hpcprobe_{module}_cluster_{suffix}",
                f"{help_text} (cluster-wide)",
                mtype,
                [("", _to_float(cluster[key]) * scale)],
            )

    # Reachability belongs in the same scrape: a node that stopped answering
    # otherwise just disappears from the series above, which reads as "fine".
    mod_lbl = f'module="{_lbl(module)}"'
    add_metric("hpcprobe_summary_nodes_total", "Nodes targeted", "gauge",
               [(mod_lbl, _to_float(meta.get("nodes_total")))])
    add_metric("hpcprobe_summary_nodes_ok", "Nodes that answered", "gauge",
               [(mod_lbl, _to_float(meta.get("nodes_ok")))])
    add_metric("hpcprobe_summary_nodes_error", "Nodes that failed to answer", "gauge",
               [(mod_lbl, _to_float(meta.get("nodes_error")))])

    return "\n".join(lines)
