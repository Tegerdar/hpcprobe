import subprocess
import sys
from typing import Any, Dict

from hpcprobe.core.output import render_or_print
from hpcprobe.formatters.summary_out import render_console_jobs

# Unlike every other subcommand, this one never touches a compute node: the
# scheduler already knows the answer, so there is no SSH fan-out and no
# `sinfo` discovery step. That makes it cheap enough to poll frequently.
#
# `-a` includes partitions the caller cannot submit to (hidden/reserved ones),
# so the totals reflect the whole scheduler rather than the caller's view.
#
# Job arrays are deliberately NOT expanded (`-r` is not passed): squeue
# reports a pending array as a single record, which is what `squeue` shows an
# operator by default. Pass --expand-arrays to count individual array tasks
# instead - on clusters that submit large arrays the two numbers differ by
# orders of magnitude.
SQUEUE_BASE = ["squeue", "-h", "-a", "-o", "%T"]

# Slurm's PENDING lumps genuinely-eligible jobs together with blocked ones
# (dependency, held, QOS/assoc limits). "idle" here means PENDING as a whole,
# which matches Moab's Idle+Blocked, not Moab's Idle alone.
RUNNING_STATES = {"RUNNING"}
PENDING_STATES = {"PENDING"}


def collect(expand_arrays: bool = False) -> Dict[str, Any]:
    """Return job counts per Slurm state, plus running/pending rollups."""
    cmd = list(SQUEUE_BASE)
    if expand_arrays:
        cmd.insert(1, "-r")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return {"error": "squeue_not_found"}
    except subprocess.TimeoutExpired:
        return {"error": "squeue_timeout"}
    except Exception as e:  # noqa: BLE001 - surfaced to the caller as data
        return {"error": str(e)}

    if result.returncode != 0:
        return {"error": (result.stderr.strip() or "squeue_failed")}

    states: Dict[str, int] = {}
    for line in result.stdout.splitlines():
        state = line.strip()
        if not state:
            continue
        states[state] = states.get(state, 0) + 1

    running = sum(v for k, v in states.items() if k in RUNNING_STATES)
    pending = sum(v for k, v in states.items() if k in PENDING_STATES)
    total = sum(states.values())

    return {
        "states": states,
        "running": running,
        "pending": pending,
        "other": total - running - pending,
        "total": total,
        "arrays_expanded": expand_arrays,
    }


def execute(args: Any) -> int:
    """Main execution router for the jobs subcommand."""
    data = collect(expand_arrays=getattr(args, "expand_arrays", False))

    if "error" in data:
        print(f"Slurm job query failed: {data['error']}", file=sys.stderr)
        return 1

    render_or_print(
        args, data, "jobs", render_console_jobs, getattr(args, "total", False)
    )
    return 0
