"""GPU telemetry with per-GPU error tracing.

The old parser called float() on every nvidia-smi field. A GPU that has fallen
off the bus reports `[N/A]`, so one bad card killed the whole cluster run with
`Fatal error: could not convert string to float: '[N/A]'`. Unreadable fields are
now None with the raw reason kept beside them - never 0.0, which would render a
dead GPU as a healthy idle one.
"""
import csv
import io
import sys
from typing import Any, Dict, List, Optional, Tuple

from hpcprobe.core.cluster import poll_cluster
from hpcprobe.core.discovery import resolve_nodes
from hpcprobe.core.output import render_or_print
from hpcprobe.core.ssh import ssh_poll

QUERY_FIELDS = (
    "index", "name", "utilization.gpu", "memory.used", "memory.total",
    "temperature.gpu", "power.draw", "pci.bus_id",
)

# ssh_poll() discards stdout on any non-zero exit, and nvidia-smi exits non-zero
# when *any* GPU is unhealthy - which would throw away the healthy rows too.
# `; echo` keeps the remote shell at 0; the real status comes off the marker.
RC_MARKER = "__SMI_RC="

NVIDIA_SMI_CMD = (
    "nvidia-smi --query-gpu=" + ",".join(QUERY_FIELDS)
    + " --format=csv,noheader,nounits; echo \"" + RC_MARKER + "$?\""
)

# Any bracketed token ([N/A], [Unknown Error], ...) is a sentinel, so new ones
# need no code change. These two spellings are unbracketed.
BARE_SENTINELS = {"", "-", "N/A", "NA", "ERR!"}

# Sensor absent on this model, not a fault - excluded from the status verdict.
SOFT_SENTINELS = {"[NOT SUPPORTED]"}

CORE_METRICS = ("util_pct", "mem_used_mb", "mem_total_mb", "temp_c", "power_w")

# Exit non-zero when the run worked but hardware is degraded. Set to 0 if
# existing scripts assume `gpu` always exits 0.
DEGRADED_EXIT_CODE = 2


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _num(value: str) -> Tuple[Optional[float], Optional[str], bool]:
    """(number, reason, is_fault) for one field.

    reason keeps nvidia-smi's raw text so `[N/A]` and `[Insufficient
    Permissions]` stay distinguishable - they need different fixes.
    """
    v = value.strip()
    upper = v.upper()
    if upper in SOFT_SENTINELS:
        return None, v, False
    if upper in BARE_SENTINELS or (v.startswith("[") and v.endswith("]")):
        return None, v or "(empty)", True
    try:
        return float(v), None, False
    except ValueError:
        return None, "unparseable: %r" % v, True


def parse_gpu_row(parts: List[str]) -> Dict[str, Any]:
    """One split nvidia-smi CSV row -> one GPU record."""
    raw = dict(zip(QUERY_FIELDS, parts))
    gpu: Dict[str, Any] = {}
    unreadable: Dict[str, str] = {}
    faults: List[str] = []

    index, reason, _ = _num(raw["index"])
    # Index comes from driver enumeration, so it survives a dead GPU. Keep the
    # raw string rather than inventing a number if even that is missing.
    gpu["index"] = int(index) if index is not None else raw["index"]
    if reason:
        unreadable["index"] = reason

    gpu["model"] = raw["name"].replace('"', "").strip() or "unknown"
    # Stable across reboots and index reshuffles - the id to quote in a ticket.
    gpu["pci_bus_id"] = raw["pci.bus_id"].strip() or "-"

    for key, field in zip(CORE_METRICS, QUERY_FIELDS[2:7]):
        value, reason, is_fault = _num(raw[field])
        gpu[key] = value
        if reason:
            unreadable[key] = reason
        if is_fault:
            faults.append(key)

    if not faults:
        gpu["status"] = "ok"
    elif len(faults) == len(CORE_METRICS):
        gpu["status"] = "unreadable"
    else:
        gpu["status"] = "partial"

    if unreadable:
        gpu["unreadable"] = unreadable
    return gpu


def parse_smi_output(stdout: str) -> Dict[str, Any]:
    """Full nvidia-smi stdout -> {"gpus": [...], ...}.

    csv.reader, not str.split(","): --format=csv quotes a model name containing
    a comma, and splitting would shift every column right.
    """
    gpus: List[Dict[str, Any]] = []
    unparsed: List[str] = []
    smi_rc: Optional[int] = None

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue

        if line.startswith(RC_MARKER):
            try:
                smi_rc = int(line[len(RC_MARKER):].strip())
            except ValueError:
                smi_rc = None
            continue

        try:
            # skipinitialspace: nvidia-smi separates with ", ", and the space
            # before a quote would otherwise stop csv treating it as a quote.
            parts = [p.strip() for p in next(csv.reader(io.StringIO(line), skipinitialspace=True))]
        except Exception:
            unparsed.append(line)
            continue

        if len(parts) != len(QUERY_FIELDS):
            # Driver warnings and banners land here instead of corrupting a row.
            unparsed.append(line)
            continue

        gpus.append(parse_gpu_row(parts))

    out: Dict[str, Any] = {"gpus": gpus}
    if unparsed:
        out["unparsed_lines"] = unparsed
    if smi_rc:
        out["smi_exit_code"] = smi_rc
    return out


# --------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------

def poll_node(node: str) -> Tuple[str, Dict[str, Any]]:
    """Fetch GPU metrics via SSH. Never raises on a bad field value."""
    result, err = ssh_poll(node, NVIDIA_SMI_CMD, fail_label="ssh_or_remote_shell_failed")
    if err:
        return node, err

    data = parse_smi_output(result.stdout)
    stderr = (result.stderr or "").strip()

    if not data["gpus"]:
        # No usable rows at all: node-level failure. Carry the real reason
        # instead of the old catch-all "ssh_auth_or_smi_failed".
        rc = data.get("smi_exit_code")
        first_line = stderr.splitlines()[0][:160] if stderr else ""
        reason = first_line or (f"nvidia-smi exited {rc}" if rc else "no GPU rows returned")
        node_err: Dict[str, Any] = {"error": reason}
        if rc:
            node_err["smi_exit_code"] = rc
        return node, node_err

    if stderr:
        data["smi_stderr"] = stderr.splitlines()[:5]
    return node, data


def collect_problems(
    data: Dict[str, Dict[str, Any]]
) -> Tuple[Dict[str, Any], List[Tuple[str, Dict[str, Any]]]]:
    """Split cluster state into (failed nodes, non-ok GPUs)."""
    node_errors: Dict[str, Any] = {}
    bad_gpus: List[Tuple[str, Dict[str, Any]]] = []

    for node in sorted(data.keys()):
        node_data = data[node]
        if "error" in node_data:
            node_errors[node] = node_data
            continue
        for gpu in node_data.get("gpus", []):
            if gpu.get("status") != "ok":
                bad_gpus.append((node, gpu))
    return node_errors, bad_gpus


def execute(args: Any) -> int:
    """Main execution router for the gpu subcommand."""
    target_nodes = resolve_nodes(args, gres_filter="gpu")
    if not target_nodes:
        print("No targets identified. Exiting.", file=sys.stderr)
        return 1

    cluster_state = poll_cluster(target_nodes, poll_node)
    render_or_print(args, cluster_state, module="gpus", console_fn=print_console)

    node_errors, bad_gpus = collect_problems(cluster_state)
    if node_errors or bad_gpus:
        # stderr, so it never contaminates -j / -c / -p on stdout.
        print_diagnostics(cluster_state, node_errors, bad_gpus)
        return DEGRADED_EXIT_CODE
    return 0


# --------------------------------------------------------------------------
# Console output
# --------------------------------------------------------------------------

def _fmt(value: Optional[float], spec: str, suffix: str = "") -> str:
    return "-" if value is None else format(value, spec) + suffix


def print_console(data: Dict[str, Dict[str, Any]]) -> None:
    """Formats the GPU data into a clean terminal table."""
    width = 112
    print("=" * width)
    print(
        f"{'Node':<12} | {'IDX':<3} | {'Model':<20} | {'Util':>6} | "
        f"{'VRAM (GB)':<13} | {'Temp':>5} | {'Power':>7} | {'Status'}"
    )
    print("=" * width)

    for node in sorted(data.keys()):
        node_data = data[node]
        if "error" in node_data:
            print(f"{node:<12} | [ ERROR: {node_data['error']} ]")
            continue

        for gpu in node_data.get("gpus", []):
            used, total = gpu.get("mem_used_mb"), gpu.get("mem_total_mb")
            vram = "-" if used is None or total is None else f"{used/1024:.1f}/{total/1024:.1f}"
            print(
                f"{node:<12} | {str(gpu['index']):<3} | {str(gpu['model'])[:20]:<20} | "
                f"{_fmt(gpu.get('util_pct'), '.1f', '%'):>6} | {vram:<13} | "
                f"{_fmt(gpu.get('temp_c'), '.0f', 'C'):>5} | "
                f"{_fmt(gpu.get('power_w'), '.1f', 'W'):>7} | {gpu.get('status', 'ok')}"
            )
    print("=" * width)


def print_diagnostics(
    data: Dict[str, Dict[str, Any]],
    node_errors: Dict[str, Any],
    bad_gpus: List[Tuple[str, Dict[str, Any]]],
) -> None:
    """Explain every failure on stderr: which node, which GPU, which field."""
    out = sys.stderr
    print("\n--- diagnostics ---", file=out)

    for node, node_data in node_errors.items():
        rc = node_data.get("smi_exit_code")
        rc_note = f" (nvidia-smi rc={rc})" if rc else ""
        print(f"{node}: no GPU data{rc_note}: {node_data['error']}", file=out)

    for node, gpu in bad_gpus:
        detail = " ".join(f"{k}={v}" for k, v in sorted(gpu.get("unreadable", {}).items()))
        print(
            f"{node} gpu{gpu['index']} [{gpu.get('pci_bus_id', '-')}] "
            f"{gpu.get('status')}: {detail or 'no detail'}",
            file=out,
        )

    for node in sorted(data.keys()):
        for line in data[node].get("smi_stderr", []):
            print(f"{node}: nvidia-smi: {line}", file=out)
        for line in data[node].get("unparsed_lines", []):
            print(f"{node}: unparsed line: {line[:160]}", file=out)

    if bad_gpus:
        # Key off what nvidia-smi actually said: "requires reset" and a card
        # that has fallen off the bus need different remedies.
        sentinels = {
            v.upper()
            for _, gpu in bad_gpus
            for v in gpu.get("unreadable", {}).values()
        }
        if any("RESET" in s for s in sentinels):
            print(
                "\nA GPU reporting [GPU requires reset] has hit a fault the driver\n"
                "cannot clear in place. It needs `nvidia-smi -r -i <index>` with no\n"
                "CUDA process attached, or a reboot. On the node:\n"
                "  dmesg -T | grep -iE 'xid|nvrm'\n"
                "  nvidia-smi -q -i <index> | head -40",
                file=out,
            )
        else:
            print(
                "\nA GPU reporting [N/A] for every metric has usually dropped off the\n"
                "PCIe bus. On the node:\n"
                "  dmesg -T | grep -iE 'xid|nvrm|pcieport'\n"
                "  nvidia-smi -q -i <index> | head -40",
                file=out,
            )
