import sys
from typing import Any, Dict, Tuple

from hpcprobe.core.cluster import poll_cluster
from hpcprobe.core.discovery import resolve_nodes
from hpcprobe.core.output import render_or_print
from hpcprobe.core.ssh import ssh_poll

NVIDIA_SMI_CMD = (
    "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,"
    "memory.total,temperature.gpu,power.draw --format=csv,noheader,nounits"
)


def poll_node(node: str) -> Tuple[str, Dict[str, Any]]:
    """Fetch real-time GPU metrics via SSH."""
    result, err = ssh_poll(node, NVIDIA_SMI_CMD, fail_label="ssh_auth_or_smi_failed")
    if err:
        return node, err

    gpus = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        gpus.append({
            "index": int(parts[0]),
            "model": parts[1].replace('"', ""),
            "util_pct": float(parts[2]) if parts[2] != "[Not Supported]" else 0.0,
            "mem_used_mb": float(parts[3]),
            "mem_total_mb": float(parts[4]),
            "temp_c": float(parts[5]),
            "power_w": float(parts[6]) if parts[6] != "[Not Supported]" else 0.0,
        })
    return node, {"gpus": gpus}


def execute(args: Any) -> int:
    """Main execution router for the gpu subcommand."""
    target_nodes = resolve_nodes(args, gres_filter="gpu")
    if not target_nodes:
        print("No targets identified. Exiting.", file=sys.stderr)
        return 1

    cluster_state = poll_cluster(target_nodes, poll_node)
    render_or_print(args, cluster_state, module="gpus", console_fn=print_console)
    return 0


def print_console(data: Dict[str, Dict[str, Any]]) -> None:
    """Formats the GPU data into a clean terminal table."""
    print("=" * 95)
    print(f"{'Node':<12} | {'IDX':<3} | {'Model':<20} | {'Util':<6} | {'VRAM (GB)':<13} | {'Temp':<4} | {'Power'}")
    print("=" * 95)

    for node in sorted(data.keys()):
        node_data = data[node]
        if "error" in node_data:
            print(f"{node:<12} | [ ERROR: {node_data['error']} ]")
            continue

        for gpu in node_data.get("gpus", []):
            vram = f"{gpu['mem_used_mb']/1024:.1f}/{gpu['mem_total_mb']/1024:.1f}"
            util = f"{gpu['util_pct']:.1f}%"
            print(
                f"{node:<12} | {gpu['index']:<3} | {gpu['model']:<20} | "
                f"{util:>6} | {vram:<13} | {gpu['temp_c']:>2.0f}°C | {gpu['power_w']:>5.1f}W"
            )
    print("=" * 95)
