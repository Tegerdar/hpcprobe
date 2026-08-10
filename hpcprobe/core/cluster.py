"""Threadpool fan-out for polling a cluster. Every command's execute() had
its own copy of this ThreadPoolExecutor/as_completed block."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Tuple

DEFAULT_MAX_WORKERS = 30


def poll_cluster(
    nodes: List[str],
    poll_fn: Callable[..., Tuple[str, Dict[str, Any]]],
    *poll_args: Any,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> Dict[str, Dict[str, Any]]:
    """Run poll_fn(node, *poll_args) for every node concurrently and collect
    the results into {node: data}.

    poll_fn must return (node, data) - matches every existing poll_node().
    """
    cluster_state: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(poll_fn, node, *poll_args): node for node in nodes}
        for future in as_completed(futures):
            node, node_data = future.result()
            cluster_state[node] = node_data
    return cluster_state
