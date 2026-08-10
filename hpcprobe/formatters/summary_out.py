"""Console rendering for `-t/--total`.

Owns the column layout for every module's summary. summarize.py produces the
numbers; nothing here computes anything beyond formatting.

Each column is (summary_key, header, width, align, fmt):
    fmt: "i" int | "f" one decimal | "pct" one decimal + % | "s" string
         "gb" one decimal + G | "w" whole watts + W
         "size" auto-scaled G/T/P | "pct?" like pct but renders -1 as "-"
"""
from typing import Any, Dict, List, Tuple

Column = Tuple[str, str, int, str, str]

COLUMNS: Dict[str, List[Column]] = {
    "gpu": [
        ("gpus", "GPUs", 5, ">", "i"),
        ("util_avg", "Util avg", 9, ">", "pct"),
        ("util_max", "Util max", 9, ">", "pct"),
        ("temp_max", "Temp max", 9, ">", "f"),
        ("power_w", "Power", 9, ">", "w"),
        ("mem_used_gb", "VRAM used", 10, ">", "gb"),
        ("mem_total_gb", "VRAM tot", 9, ">", "gb"),
        ("mem_used_pct", "VRAM%", 7, ">", "pct"),
    ],
    "cpu": [
        ("cpus_total", "CPUs", 6, ">", "i"),
        ("cpus_alloc", "Alloc", 6, ">", "i"),
        ("cpus_idle", "Idle", 6, ">", "i"),
        ("alloc_pct", "Alloc%", 8, ">", "pct"),
        ("load", "Load", 8, ">", "f"),
        ("sockets", "Sockets", 8, ">", "i"),
    ],
    "mem": [
        ("os_total_gb", "OS total", 9, ">", "gb"),
        ("os_avail_gb", "OS avail", 9, ">", "gb"),
        ("os_used_pct", "OS used%", 9, ">", "pct"),
        ("slurm_real_gb", "Real", 9, ">", "gb"),
        ("slurm_alloc_gb", "Alloc", 9, ">", "gb"),
        ("slurm_alloc_pct", "Alloc%", 8, ">", "pct"),
    ],
    "net": [
        ("ports", "Ports", 6, ">", "i"),
        ("ports_up", "Up", 5, ">", "i"),
        ("ports_down", "Down", 6, ">", "i"),
        ("out_of_buffer", "OutOfBuf", 10, ">", "i"),
        ("errors", "Errors", 9, ">", "i"),
        ("link_down_events", "LinkDown", 9, ">", "i"),
        ("pause_active", "Pause", 6, ">", "i"),
    ],
    "stg": [
        ("mounts", "Mounts", 7, ">", "i"),
        ("size_gb", "Size", 10, ">", "size"),
        ("used_gb", "Used", 10, ">", "size"),
        ("avail_gb", "Avail", 10, ">", "size"),
        ("use_pct", "Use%", 7, ">", "pct"),
        ("worst_pct", "Worst%", 8, ">", "pct?"),
        ("beegfs_targets", "BeeGFS", 7, ">", "i"),
        ("beegfs_free_pct_min", "BGFSfree", 9, ">", "pct?"),
        ("lustre_targets", "Lustre", 7, ">", "i"),
        ("lustre_use_pct_max", "LustreUse", 10, ">", "pct?"),
    ],
}

NODE_COL_WIDTH = 14


def _fmt_size(gb: float) -> str:
    """Auto-scale GB -> TB/PB so parallel-filesystem totals stay readable."""
    if gb >= 1024 * 1024:
        return f"{gb / (1024 * 1024):.1f}P"
    if gb >= 1024:
        return f"{gb / 1024:.1f}T"
    return f"{gb:.1f}G"


def _fmt(value: Any, kind: str) -> str:
    if value is None:
        return "-"
    if kind == "s":
        return str(value)
    if kind == "gb":
        return f"{float(value):.1f}G"
    if kind == "w":
        return f"{float(value):.0f}W"
    if kind == "size":
        return _fmt_size(float(value))
    if kind == "pct?":
        return "-" if float(value) < 0 else f"{float(value):.1f}%"
    if kind == "pct":
        return f"{float(value):.1f}%"
    if kind == "i":
        return str(int(value))
    return f"{float(value):.1f}"


def _row(label: str, values: Dict[str, Any], cols: List[Column]) -> str:
    cells = [f"{label:<{NODE_COL_WIDTH}}"]
    for key, _hdr, width, align, kind in cols:
        cells.append(f"{_fmt(values.get(key), kind):{align}{width}}")
    return " | ".join(cells)


def _header(cols: List[Column]) -> str:
    cells = [f"{'Node':<{NODE_COL_WIDTH}}"]
    for _key, hdr, width, align, _kind in cols:
        cells.append(f"{hdr:{align}{width}}")
    return " | ".join(cells)


def render_console(summary: Dict[str, Any], module: str) -> None:
    cols = COLUMNS[module]
    header = _header(cols)
    width = len(header)

    print("=" * width)
    print(header)
    print("=" * width)

    nodes = summary.get("nodes", {})
    for node in sorted(nodes):
        print(_row(node[:NODE_COL_WIDTH], nodes[node], cols))

    meta = summary.get("meta", {})
    for node, reason in sorted(meta.get("errors", {}).items()):
        print(f"{node[:NODE_COL_WIDTH]:<{NODE_COL_WIDTH}} | [ ERROR: {reason} ]")

    print("-" * width)
    print(_row("CLUSTER", summary.get("cluster", {}), cols))
    print("=" * width)

    line = (f"{meta.get('nodes_ok', 0)}/{meta.get('nodes_total', 0)} nodes reporting")
    if meta.get("nodes_error"):
        line += f", {meta['nodes_error']} unreachable"
    if module == "stg":
        dropped = summary.get("cluster", {}).get("shared_mounts_deduped", 0)
        if dropped:
            line += (f"; {dropped} shared-filesystem mount(s) counted once "
                     f"cluster-wide, not per node")
    if module == "gpu":
        line += "; cluster Util avg is weighted per GPU"
    if module == "cpu":
        line += "; cluster Load is the mean of per-node loads"
    print(f"({line})")


def render_console_jobs(data: Dict[str, Any], total: bool = False) -> None:
    """Jobs has no per-node dimension, so `-t` just tightens the output."""
    running = data.get("running", 0)
    pending = data.get("pending", 0)
    other = data.get("other", 0)

    if total:
        print(f"running={running} pending={pending} other={other} "
              f"total={data.get('total', 0)}")
        return

    print("=" * 34)
    print(f"{'State':<20} | {'Jobs':>9}")
    print("=" * 34)
    for state, count in sorted(data.get("states", {}).items()):
        print(f"{state[:20]:<20} | {count:>9}")
    print("-" * 34)
    print(f"{'RUNNING':<20} | {running:>9}")
    print(f"{'PENDING (idle)':<20} | {pending:>9}")
    if other:
        print(f"{'other states':<20} | {other:>9}")
    print(f"{'TOTAL':<20} | {data.get('total', 0):>9}")
    print("=" * 34)
