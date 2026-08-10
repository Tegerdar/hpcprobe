import subprocess
from typing import Any, Dict, Iterable


def query_node_state(node: str, keys: Iterable[str]) -> Dict[str, Any]:
    """Return the requested `key=value` fields from `scontrol show node
    <node>`, lowercased and prefixed with 'slurm_' (e.g. State -> slurm_state).

    Runs locally (this queries the scheduler, not the remote node over SSH),
    matching the original behaviour in cpu.py/mem.py.
    """
    keys = set(keys)
    try:
        result = subprocess.run(
            ["scontrol", "show", "node", node], capture_output=True, text=True
        )
        if result.returncode != 0:
            return {"slurm_status": "Not in Slurm"}

        data: Dict[str, Any] = {}
        for word in result.stdout.split():
            if "=" in word:
                key, value = word.split("=", 1)
                if key in keys:
                    data[f"slurm_{key.lower()}"] = value
        return data
    except Exception as e:
        return {"slurm_error": str(e)}
