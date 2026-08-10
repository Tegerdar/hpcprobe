import socket
import subprocess
import sys
from typing import List, Optional


def discover_nodes(gres_filter: Optional[str] = None) -> List[str]:
    """Discover compute nodes via `sinfo`.

    gres_filter: if given (e.g. "gpu"), only nodes whose GRES string contains
    it (case-insensitive) are returned - this is what gpu.py used to do with
    its own copy of this function. Leave it None for "all nodes", which is
    what cpu/mem/network/storage want.
    """
    fmt = "%n|%G" if gres_filter else "%n"
    try:
        result = subprocess.run(
            ["sinfo", "-N", "-h", "-o", fmt],
            capture_output=True, text=True, check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f"Slurm discovery failed: {e}", file=sys.stderr)
        return []

    nodes = set()
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        if gres_filter:
            node, gres = line.split("|", 1)
            if gres_filter.lower() in gres.lower():
                nodes.add(node.strip())
        else:
            nodes.add(line.strip())
    return sorted(nodes)


def resolve_nodes(args, gres_filter: Optional[str] = None) -> List[str]:
    """`-n/--nodes` has three states, distinguished by argparse's nargs='*':

      - flag absent            -> args.nodes is None   -> run Slurm discovery
      - `-n node1 node2 ...`   -> args.nodes is a list  -> use it verbatim
      - `-n` with no names     -> args.nodes is []      -> target only the
                                   host hpcprobe is running on, no SSH involved

    The bare-flag case exists for service accounts that can reach this host
    directly (see core/ssh.py's local short-circuit) but have no SSH access
    to anything else - `hpcprobe gpu -n -t -p` never touches the network.

    Like an explicit node list, the bare case bypasses gres_filter: if
    you're running it on this host, you already know what's on it.
    """
    explicit = getattr(args, "nodes", None)
    if explicit is None:
        return discover_nodes(gres_filter)
    if len(explicit) == 0:
        return [socket.gethostname().split(".", 1)[0]]
    return explicit
