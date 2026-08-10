import argparse
import sys

from hpcprobe.commands import cpu, gpu, jobs, mem, net, stg

HANDLERS = {
    "gpu": gpu,
    "cpu": cpu,
    "mem": mem,
    "net": net,
    "stg": stg,
    "jobs": jobs,
}

NODES_HELP = (
    "Target nodes. Omit for Slurm discovery. Give names to target exactly "
    "those nodes. Pass with no names (just -n) to target only the host "
    "hpcprobe is running on, with no SSH involved."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="hpcprobe: Modern HPC Cluster Administration & Telemetry",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
GLOBAL OPTIONS:
  These flags can be appended to any subcommand:
    -t, --total       Collapse the detail rows: one aggregate row per node,
                      plus a single cluster-wide row. Applies to every output
                      format, so -t -p exports aggregate metrics only.
    -j, --json        Output in machine-readable JSON
    -c, --csv         Output in flattened CSV format
    -p, --prometheus  Output in Prometheus OpenMetrics format

  Example:
    hpcprobe gpu -t
    hpcprobe stg -t -p
    hpcprobe jobs
        """,
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        help="Target subsystem to query",
    )

    # --- Subcommand definitions ---
    parser_gpu = subparsers.add_parser(
        "gpu",
        help="Real-time GPU hardware telemetry via SSH",
    )
    parser_gpu.add_argument("-n", "--nodes", nargs="*", metavar="NODE", help=NODES_HELP)

    parser_cpu = subparsers.add_parser(
        "cpu",
        help="CPU state",
    )
    parser_cpu.add_argument("-n", "--nodes", nargs="*", metavar="NODE", help=NODES_HELP)
    parser_cpu.add_argument(
        "-e", "--extended",
        action="store_true",
        help="Include exhaustive CPU parameters (flags, vulnerabilities, NUMA mapping)",
    )

    parser_mem = subparsers.add_parser(
        "mem",
        help="Memory usage and state",
    )
    parser_mem.add_argument("-n", "--nodes", nargs="*", metavar="NODE", help=NODES_HELP)
    parser_mem.add_argument(
        "-e", "--extended",
        action="store_true",
        help="Include every /proc/meminfo field, not just the common ones",
    )

    parser_net = subparsers.add_parser(
        "net",
        help="InfiniBand/RoCE link state and NIC error counters",
    )
    parser_net.add_argument("-n", "--nodes", nargs="*", metavar="NODE", help=NODES_HELP)
    parser_net.add_argument(
        "-e", "--extended",
        action="store_true",
        help="Include full per-netdev counters and interface details",
    )
    parser_net.add_argument(
        "-d", "--delta",
        action="store_true",
        help="Show counter change since the last 'net' run (snapshot-based; "
             "first run establishes the baseline)",
    )

    parser_stg = subparsers.add_parser(
        "stg",
        help="Filesystem usage (df) plus BeeGFS/Lustre target-level detail",
    )
    parser_stg.add_argument("-n", "--nodes", nargs="*", metavar="NODE", help=NODES_HELP)
    parser_stg.add_argument(
        "-e", "--extended",
        action="store_true",
        help="Include full per-mount raw details",
    )

    parser_jobs = subparsers.add_parser(
        "jobs",
        help="Scheduler queue depth: running and pending (idle) job counts",
    )
    parser_jobs.add_argument(
        "--expand-arrays",
        action="store_true",
        help="Count individual array tasks instead of one record per job array",
    )

    # --- Standardized global options ---
    all_parsers = (parser_gpu, parser_cpu, parser_mem, parser_net, parser_stg, parser_jobs)
    for subp in all_parsers:
        subp.add_argument(
            "-t", "--total",
            action="store_true",
            help="Aggregate view: one row per node plus a cluster total row",
        )
        format_group = subp.add_mutually_exclusive_group()
        format_group.add_argument("-j", "--json", action="store_true", help="Output in machine-readable JSON")
        format_group.add_argument("-c", "--csv", action="store_true", help="Output in flattened CSV format")
        format_group.add_argument("-p", "--prometheus", action="store_true", help="Output in Prometheus OpenMetrics format")

    return parser


def main() -> None:
    args = build_parser().parse_args()

    try:
        sys.exit(HANDLERS[args.command].execute(args))
    except KeyboardInterrupt:
        print("\nExecution interrupted by user.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"\nFatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
