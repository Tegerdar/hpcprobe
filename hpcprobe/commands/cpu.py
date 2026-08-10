import json
import sys
from typing import Any, Dict, Tuple

from hpcprobe.core.cluster import poll_cluster
from hpcprobe.core.discovery import resolve_nodes
from hpcprobe.core.output import render_or_print
from hpcprobe.core.slurm import query_node_state
from hpcprobe.core.ssh import ssh_poll

SLURM_KEYS = {"State", "CPUTot", "CPULoad", "AllocCPUs", "IdleCPUs"}


def poll_node(node: str, extended: bool) -> Tuple[str, Dict[str, Any]]:
    """Fetch real-time CPU metrics via SSH and local Slurm query."""
    result, err = ssh_poll(node, "lscpu -J", fail_label="ssh_auth_or_lscpu_failed")
    if err:
        hw_data = err
    else:
        try:
            lscpu_data = json.loads(result.stdout)
            common_keys = {
                "model_name", "architecture", "cpu(s)", "thread(s)_per_core",
                "core(s)_per_socket", "socket(s)", "numa_node(s)",
            }
            hw_data = {}
            for item in lscpu_data.get("lscpu", []):
                field = item.get("field", "").replace(":", "").strip().lower().replace(" ", "_").replace("-", "_")
                if extended or field in common_keys:
                    hw_data[field] = item.get("data")
        except Exception as e:
            hw_data = {"error": str(e)}

    # Slurm's view of CPU state comes from the local scheduler, not another SSH round-trip.
    slurm_data = query_node_state(node, SLURM_KEYS)

    return node, {**hw_data, **slurm_data}


def execute(args: Any) -> int:
    """Main execution router for the cpu subcommand."""
    target_nodes = resolve_nodes(args)
    if not target_nodes:
        print("No targets identified. Exiting.", file=sys.stderr)
        return 1

    extended = getattr(args, "extended", False)
    cluster_state = poll_cluster(target_nodes, poll_node, extended)
    render_or_print(args, cluster_state, "cpu", print_console, extended)
    return 0


def print_console(data: Dict[str, Dict[str, Any]], extended: bool = False) -> None:
    """Formats the CPU data into a clean terminal table."""
    print("=" * 115)
    print(f"{'Node':<12} | {'State':<12} | {'CPU Model':<30} | {'CPUs (A/I/T)':<14} | {'Load':<6} | {'Sockets':<7} | {'NUMA':<4}")
    print("=" * 115)

    for node in sorted(data.keys()):
        node_data = data[node]

        if "error" in node_data and "slurm_error" in node_data:
            print(f"{node:<12} | [ ERROR: {node_data.get('error', 'Unknown Error')} ]")
            continue

        state = node_data.get("slurm_state", "UNKNOWN")[:12]

        if "error" in node_data:
            model = f"SSH ERR: {node_data['error']}"[:30]
            cpus_ait = "-/-/-"
            load = "-"
            sockets = "-"
            numa = "-"
        else:
            model = str(node_data.get("model_name", "Unknown"))[:30]
            sockets = str(node_data.get("socket(s)", "-"))
            numa = str(node_data.get("numa_node(s)", "-"))

            alloc = node_data.get("slurm_alloccpus", "-")
            idle = node_data.get("slurm_idlecpus", "-")
            total = node_data.get("slurm_cputot", node_data.get("cpu(s)", "-"))
            cpus_ait = f"{alloc}/{idle}/{total}"
            load = str(node_data.get("slurm_cpuload", "-"))

        print(f"{node:<12} | {state:<12} | {model:<30} | {cpus_ait:<14} | {load:<6} | {sockets:<7} | {numa:<4}")

    print("=" * 115)

    if extended:
        print("\n[ Extended Parameters ]")
        for node in sorted(data.keys()):
            node_data = data[node]
            print(f"\n--- {node} ---")
            for key, value in node_data.items():
                print(f"  {key:<35} : {value}")
