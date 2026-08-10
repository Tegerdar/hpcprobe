import sys
from typing import Any, Dict, Tuple

from hpcprobe.core.cluster import poll_cluster
from hpcprobe.core.discovery import resolve_nodes
from hpcprobe.core.output import render_or_print
from hpcprobe.core.slurm import query_node_state
from hpcprobe.core.ssh import ssh_poll

SLURM_KEYS = {"State", "RealMemory", "AllocMem", "FreeMem"}


def poll_node(node: str, extended: bool) -> Tuple[str, Dict[str, Any]]:
    """Fetch real-time Memory metrics via SSH and local Slurm query."""
    # /proc/meminfo instead of `free` avoids output-parsing differences across distros.
    result, err = ssh_poll(node, "cat /proc/meminfo", fail_label="ssh_auth_or_meminfo_failed")
    if err:
        hw_data = err
    else:
        hw_data = {}
        common_keys = {"MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached", "SwapTotal", "SwapFree"}
        for line in result.stdout.strip().split("\n"):
            if ":" not in line:
                continue
            key, value_str = line.split(":", 1)
            key = key.strip()

            if extended or key in common_keys:
                val_parts = value_str.strip().split()
                if val_parts:
                    try:
                        # Convert kB to MB for easier baseline readability
                        mb_val = int(val_parts[0]) / 1024
                        hw_data[f"os_{key.lower()}_mb"] = round(mb_val, 2)
                    except ValueError:
                        hw_data[f"os_{key.lower()}"] = value_str.strip()

    # Slurm's view of memory comes from the local scheduler, not another SSH round-trip.
    slurm_data = query_node_state(node, SLURM_KEYS)

    return node, {**hw_data, **slurm_data}


def execute(args: Any) -> int:
    """Main execution router for the mem subcommand."""
    target_nodes = resolve_nodes(args)
    if not target_nodes:
        print("No targets identified. Exiting.", file=sys.stderr)
        return 1

    extended = getattr(args, "extended", False)
    cluster_state = poll_cluster(target_nodes, poll_node, extended)
    render_or_print(args, cluster_state, "memory", print_console, extended)
    return 0


def print_console(data: Dict[str, Dict[str, Any]], extended: bool = False) -> None:
    """Formats the memory data into a clean terminal table."""
    print("=" * 110)
    print(f"{'Node':<12} | {'State':<12} | {'OS Total':<10} | {'OS Avail':<10} | {'Slurm Real':<10} | {'Slurm Alloc':<11} | {'Slurm Free'}")
    print("=" * 110)

    for node in sorted(data.keys()):
        node_data = data[node]

        if "error" in node_data and "slurm_error" in node_data:
            print(f"{node:<12} | [ ERROR: {node_data.get('error', 'Unknown Error')} ]")
            continue

        state = node_data.get("slurm_state", "UNKNOWN")[:12]

        if "error" in node_data:
            os_total = f"ERR: {node_data['error']}"[:10]
            os_avail = "-"
        else:
            # Convert OS MB to GB for the display table
            total_mb = node_data.get("os_memtotal_mb", 0)
            avail_mb = node_data.get("os_memavailable_mb", 0)
            os_total = f"{total_mb / 1024:.1f}G" if total_mb else "-"
            os_avail = f"{avail_mb / 1024:.1f}G" if avail_mb else "-"

        # Slurm parameters (Native MB parsed as GB for display)
        try:
            slurm_real = f"{float(node_data.get('slurm_realmemory', 0)) / 1024:.1f}G"
            slurm_alloc = f"{float(node_data.get('slurm_allocmem', 0)) / 1024:.1f}G"
            slurm_free = f"{float(node_data.get('slurm_freemem', 0)) / 1024:.1f}G"
        except ValueError:
            slurm_real = node_data.get("slurm_realmemory", "-")
            slurm_alloc = node_data.get("slurm_allocmem", "-")
            slurm_free = node_data.get("slurm_freemem", "-")

        print(f"{node:<12} | {state:<12} | {os_total:>10} | {os_avail:>10} | {slurm_real:>10} | {slurm_alloc:>11} | {slurm_free:>10}")

    print("=" * 110)

    if extended:
        print("\n[ Extended Parameters ]")
        for node in sorted(data.keys()):
            node_data = data[node]
            print(f"\n--- {node} ---")
            for key, value in node_data.items():
                print(f"  {key:<35} : {value}")
