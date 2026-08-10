from typing import Any, Callable, Dict

from hpcprobe.core.summarize import normalise_module, summarize
from hpcprobe.formatters import csv_out, json_out, prometheus_out, summary_out


def render_or_print(
    args: Any,
    cluster_state: Dict[str, Dict[str, Any]],
    module: str,
    console_fn: Callable[..., None],
    *console_args: Any,
) -> None:
    module_norm = normalise_module(module)

    # `-t` replaces the detailed view everywhere, machine formats included, so
    # a scraper configured with -t never sees the per-GPU/per-mount rows.
    # `jobs` is already a cluster-level answer and has nothing to collapse, so
    # it keeps its own renderer and handles -t as a console-width option.
    if getattr(args, "total", False) and module_norm != "jobs":
        summary = summarize(cluster_state, module)
        if getattr(args, "prometheus", False):
            print(prometheus_out.render_summary(summary, module_norm))
        elif getattr(args, "csv", False):
            print(csv_out.render_summary(summary, module_norm))
        elif getattr(args, "json", False):
            print(json_out.render(summary))
        else:
            summary_out.render_console(summary, module_norm)
        return

    if getattr(args, "prometheus", False):
        print(prometheus_out.render(cluster_state, module=module))
    elif getattr(args, "csv", False):
        print(csv_out.render(cluster_state, module=module))
    elif getattr(args, "json", False):
        print(json_out.render(cluster_state, module=module))
    else:
        console_fn(cluster_state, *console_args)
