import re
import sys
from typing import Any, Dict, List, Tuple

from hpcprobe.core.cluster import poll_cluster
from hpcprobe.core.discovery import resolve_nodes
from hpcprobe.core.output import render_or_print
from hpcprobe.core.ssh import ssh_poll

SSH_TIMEOUT = 5  # storage queries (beegfs-df, lfs df) can be slower than sysfs reads

# Pseudo-filesystems to exclude entirely - never real storage capacity.
DF_SKIP_FSTYPES = {
    "tmpfs", "devtmpfs", "proc", "sysfs", "cgroup", "cgroup2", "overlay",
    "squashfs", "devpts", "autofs", "mqueue", "debugfs", "tracefs",
    "securityfs", "pstore", "hugetlbfs", "nsfs", "rpc_pipefs", "fusectl",
}

# Mounts below this size are real (e.g. /boot/efi, efivarfs) but rarely what
# anyone is checking capacity on. Hidden from the default table to keep it
# scannable; always present in --extended and in JSON/CSV/Prometheus output.
DEFAULT_TABLE_MIN_GB = 3.0

# Remote-side collector. Root-free by design (matches the pattern used by
# mem.py / cpu.py / net.py). Three record types, one line each:
#   MOUNT|<raw whitespace-separated `df -PT` line: source fstype 1k_blocks used avail pcent mountpoint>
#   BEEGFS_SECTION|<meta|storage>
#   BEEGFS_ROW|<raw beegfs-df row text>
#   LUSTRE_ROW|<raw lfs df -h row text>
#
# Only the record-type tag is pipe-delimited; each payload is passed through
# verbatim and split on whitespace locally (see _parse_remote_output), since
# that's the actual column separator these tools emit.
#
# beegfs-df and lfs df output is intentionally passed through as raw text
# rather than pre-parsed remotely: their column layout varies across BeeGFS
# versions (some include a "Cap." column, some don't) and lfs df formatting
# varies by Lustre version too. Parsing happens locally in Python where it's
# easier to be defensive about layout differences and version drift.
REMOTE_SCRIPT = r"""
df -PT 2>/dev/null | tail -n +2 | while IFS= read -r line; do
  echo "MOUNT|${line}"
done

if command -v beegfs-df >/dev/null 2>&1; then
  section="unknown"
  beegfs-df 2>/dev/null | while IFS= read -r line; do
    case "$line" in
      *METADATA*SERVERS*) echo "BEEGFS_SECTION|meta"; continue ;;
      *STORAGE*TARGETS*) echo "BEEGFS_SECTION|storage"; continue ;;
      TargetID*|========*|"") continue ;;
    esac
    echo "BEEGFS_ROW|${line}"
  done
fi

if command -v lfs >/dev/null 2>&1; then
  lfs df -h 2>/dev/null | while IFS= read -r line; do
    trimmed=$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    case "$trimmed" in
      UUID*|"") continue ;;
    esac
    echo "LUSTRE_ROW|${line}"
  done
fi
"""

# Defensive beegfs-df row matcher. Handles both the classic layout
# (TargetID Pool Total Free % ITotal IFree %) and the newer one that adds a
# "Cap." column before Pool - the optional non-greedy token absorbs it when
# present without requiring an exact column count.
_BEEGFS_ROW_RE = re.compile(
    r"^\s*(?P<target_id>\d+)\s+"
    r"(?:\S+\s+)?"                            # optional Cap. column
    r"(?P<pool>\[?\w+\]?)\s+"
    r"(?P<total>[\d.]+\s*[KMGTP]i?B)\s+"
    r"(?P<free>[\d.]+\s*[KMGTP]i?B)\s+"
    r"(?P<free_pct>\d+)%\s+"
    r"(?P<itotal>[\d.]+M)\s+"
    r"(?P<ifree>[\d.]+M)\s+"
    r"(?P<ifree_pct>\d+)%\s*$"
)

# lfs df -h row format (both MDT and OST lines share this shape):
#   fsname-MDTxxxx_UUID   <total>  <used>  <avail>  <use%>  <mount>[state]
_LUSTRE_ROW_RE = re.compile(
    r"^\s*(?P<target>\S+)\s+"
    r"(?P<total>[\d.]+[KMGTP]?)\s+"
    r"(?P<used>[\d.]+[KMGTP]?)\s+"
    r"(?P<avail>[\d.]+[KMGTP]?)\s+"
    r"(?P<use_pct>\d+)%\s+"
    r"(?P<mount>\S+)"
    r"(?:\[(?P<state>[^\]]*)\])?\s*$"
)

# lfs df -h prints one extra row at the end with no [MDT:N]/[OST:N] suffix and
# no bracketed state - the aggregate across all targets for the filesystem:
#   filesystem_summary:   <total>  <used>  <avail>  <use%>  <mount>
_LUSTRE_SUMMARY_RE = re.compile(
    r"^\s*filesystem_summary:\s+"
    r"(?P<total>[\d.]+[KMGTP]?)\s+"
    r"(?P<used>[\d.]+[KMGTP]?)\s+"
    r"(?P<avail>[\d.]+[KMGTP]?)\s+"
    r"(?P<use_pct>\d+)%\s+"
    r"(?P<mount>\S+)\s*$"
)


def _parse_beegfs_row(raw: str) -> Dict[str, Any]:
    if raw.strip().startswith("[ERROR"):
        return {"error": raw.strip()}
    m = _BEEGFS_ROW_RE.match(raw)
    if m:
        d = m.groupdict()
        d["free_pct"] = int(d["free_pct"])
        d["ifree_pct"] = int(d["ifree_pct"])
        return d
    return {"unparsed": raw.strip()}


def _parse_lustre_row(raw: str) -> Dict[str, Any]:
    m = _LUSTRE_SUMMARY_RE.match(raw)
    if m:
        d = m.groupdict()
        d["is_summary"] = True
        try:
            d["use_pct"] = int(d["use_pct"])
        except (TypeError, ValueError):
            pass
        return d
    m = _LUSTRE_ROW_RE.match(raw)
    if m:
        d = m.groupdict()
        try:
            d["use_pct"] = int(d["use_pct"])
        except (TypeError, ValueError):
            pass
        return d
    return {"unparsed": raw.strip()}


def _parse_remote_output(stdout: str) -> Dict[str, Any]:
    """Turn the delimited remote script output into a structured dict:
    {
        "mounts": [ {source, fstype, blocks_1k, used_1k, avail_1k, pcent, mountpoint}, ... ],
        "beegfs": {"meta": [row, ...], "storage": [row, ...]},
        "lustre": [row, ...],
    }
    """
    mounts = []
    beegfs: Dict[str, List[Dict[str, Any]]] = {"meta": [], "storage": []}
    lustre = []
    beegfs_section = None

    for line in stdout.strip().split("\n"):
        if not line or "|" not in line:
            continue
        record, _, rest = line.partition("|")

        if record == "MOUNT":
            fields = rest.split(None, 6)
            if len(fields) != 7:
                continue
            source, fstype, blocks, used, avail, pcent, mountpoint = fields
            if fstype in DF_SKIP_FSTYPES:
                continue
            mounts.append({
                "source": source,
                "fstype": fstype,
                "blocks_1k": blocks,
                "used_1k": used,
                "avail_1k": avail,
                "pcent": pcent,
                "mountpoint": mountpoint,
            })

        elif record == "BEEGFS_SECTION":
            beegfs_section = rest.strip()

        elif record == "BEEGFS_ROW":
            target = "meta" if beegfs_section == "meta" else "storage"
            beegfs[target].append(_parse_beegfs_row(rest))

        elif record == "LUSTRE_ROW":
            lustre.append(_parse_lustre_row(rest))

    # lfs df -h repeats its entire target listing once per active mountpoint
    # when a node has the same Lustre filesystem mounted at multiple paths
    # (e.g. /lustre-storage, /apps, /home all backed by one 'rtu' filesystem).
    # Dedup on target name alone (not mount) - the same physical MDT/OST
    # reported from different local mountpoints has identical capacity
    # numbers, so only the target identity matters for uniqueness.
    seen = set()
    deduped_lustre = []
    for row in lustre:
        if "unparsed" in row:
            deduped_lustre.append(row)  # never dedup unparsed - each is diagnostic signal
            continue
        key = row.get("target") if "target" in row else \
            ("summary", row.get("total"), row.get("used"), row.get("avail"))
        if key in seen:
            continue
        seen.add(key)
        deduped_lustre.append(row)

    return {"mounts": mounts, "beegfs": beegfs, "lustre": deduped_lustre}


def poll_node(node: str) -> Tuple[str, Dict[str, Any]]:
    """Fetch generic mount usage plus BeeGFS/Lustre target detail via SSH.

    Root-free: `df`, `beegfs-df`, and `lfs df` are all readable without
    elevated privileges. If beegfs-df / lfs are not installed on a node, those
    sections are simply empty rather than an error - this is expected on
    nodes that aren't BeeGFS/Lustre clients.
    """
    result, err = ssh_poll(
        node, REMOTE_SCRIPT, timeout=SSH_TIMEOUT, extra_timeout=3,
        fail_label="ssh_auth_or_storage_query_failed",
    )
    if err:
        return node, err
    return node, _parse_remote_output(result.stdout)


def execute(args: Any) -> int:
    """Main execution router for the stg subcommand."""
    target_nodes = resolve_nodes(args)
    if not target_nodes:
        print("No targets identified. Exiting.", file=sys.stderr)
        return 1

    extended = getattr(args, "extended", False)
    cluster_state = poll_cluster(target_nodes, poll_node)
    render_or_print(args, cluster_state, "stg", print_console, extended)
    return 0


def _blocks_to_gb(blocks_1k: str) -> float:
    try:
        return int(blocks_1k) / (1024 * 1024)
    except ValueError:
        return 0.0


def _fmt_size(gb: float) -> str:
    """Auto-scale to TB above 1024 GB so large parallel-fs mounts (Lustre,
    BeeGFS) don't render as unreadable 6-digit GB numbers."""
    if gb >= 1024:
        return f"{gb / 1024:.1f}T"
    return f"{gb:.1f}G"


def _pcent_int(pcent: str) -> int:
    try:
        return int(pcent.rstrip("%"))
    except (ValueError, AttributeError):
        return -1


def print_console(data: Dict[str, Dict[str, Any]], extended: bool = False) -> None:
    """Formats the storage data into a clean terminal table."""
    width = 88
    print("=" * width)
    print(f"{'Node':<12} | {'Mount':<24} | {'FSType':<10} | {'Size':>7} | {'Used':>7} | {'Avail':>7} | {'Use%'}")
    print("=" * width)

    for node in sorted(data.keys()):
        node_data = data[node]

        if "error" in node_data:
            print(f"{node:<12} | [ ERROR: {node_data['error']} ]")
            continue

        mounts = node_data.get("mounts", [])
        shown_any = False

        for m in mounts:
            size_gb = _blocks_to_gb(m["blocks_1k"])
            used_gb = _blocks_to_gb(m["used_1k"])
            avail_gb = _blocks_to_gb(m["avail_1k"])
            use_pct = _pcent_int(m["pcent"])

            if not extended and size_gb < DEFAULT_TABLE_MIN_GB:
                continue

            shown_any = True
            marker = "  <-- LOW SPACE" if use_pct >= 90 else ""
            print(
                f"{node:<12} | {m['mountpoint']:<24} | {m['fstype']:<10} | "
                f"{_fmt_size(size_gb):>7} | {_fmt_size(used_gb):>7} | {_fmt_size(avail_gb):>7} | "
                f"{m['pcent']:>4}{marker}"
            )

        if not shown_any and mounts:
            print(f"{node:<12} | [ only small/system mounts - use -e to show ]")

    print("=" * width)
    if not extended:
        print(f"(mounts under {DEFAULT_TABLE_MIN_GB:.0f}G hidden - use -e to show all)")

    # BeeGFS / Lustre target-level detail - shown whenever present, since this
    # is the whole point of the module (capacity pool / target-level free%,
    # not just the client-side mount aggregate that df alone would show).
    has_beegfs = any(
        node_data.get("beegfs", {}).get("meta") or node_data.get("beegfs", {}).get("storage")
        for node_data in data.values() if "error" not in node_data
    )
    has_lustre = any(
        node_data.get("lustre") for node_data in data.values() if "error" not in node_data
    )

    if has_beegfs:
        print("\n[ BeeGFS Targets ]")
        for node in sorted(data.keys()):
            node_data = data[node]
            if "error" in node_data:
                continue
            beegfs = node_data.get("beegfs", {})
            if not beegfs.get("meta") and not beegfs.get("storage"):
                continue
            print(f"\n  {node}:")
            for kind, rows in (("Metadata", beegfs.get("meta", [])), ("Storage", beegfs.get("storage", []))):
                if not rows:
                    continue
                print(f"    {kind}")
                for r in rows:
                    if "error" in r:
                        print(f"      [ERROR] {r['error']}")
                    elif "unparsed" in r:
                        print(f"      [unparsed] {r['unparsed']}")
                    else:
                        marker = "  <-- LOW FREE%" if r["free_pct"] <= 10 else ""
                        print(
                            f"      #{r['target_id']:<5} {r['pool']:<10} "
                            f"{r['total']:>9} total  {r['free']:>9} free  "
                            f"({r['free_pct']:>3}% free){marker}"
                        )

    if has_lustre:
        print("\n[ Lustre Targets ]")
        for node in sorted(data.keys()):
            node_data = data[node]
            if "error" in node_data:
                continue
            rows = node_data.get("lustre", [])
            if not rows:
                continue
            print(f"\n  {node}:")
            target_rows = [r for r in rows if not r.get("is_summary") and "unparsed" not in r]
            unparsed_rows = [r for r in rows if "unparsed" in r]
            summary_rows = [r for r in rows if r.get("is_summary")]

            mdt_rows = [r for r in target_rows if "MDT" in r.get("target", "").upper()]
            ost_rows = [r for r in target_rows if "OST" in r.get("target", "").upper()]
            other_rows = [r for r in target_rows if r not in mdt_rows and r not in ost_rows]

            def _print_target_row(r: Dict[str, Any]) -> None:
                use_pct = r.get("use_pct", -1)
                marker = "  <-- LOW SPACE" if isinstance(use_pct, int) and use_pct >= 90 else ""
                print(
                    f"      {r['target']:<26} {r['total']:>7} total  {r['used']:>7} used  "
                    f"{r['avail']:>7} avail  ({r['use_pct']:>3}% used){marker}"
                )

            for r in mdt_rows:
                _print_target_row(r)

            if ost_rows:
                use_pcts = [r["use_pct"] for r in ost_rows if isinstance(r.get("use_pct"), int)]
                uniform = use_pcts and (max(use_pcts) - min(use_pcts) <= 2) and not extended
                any_hot = any(p >= 90 for p in use_pcts) if use_pcts else False

                if uniform and not any_hot:
                    lo, hi = min(use_pcts), max(use_pcts)
                    pct_str = f"{lo}%" if lo == hi else f"{lo}-{hi}%"
                    print(
                        f"      {len(ost_rows)} OSTs (0..{len(ost_rows)-1}){'':<3} "
                        f"~{ost_rows[0]['total']:>7} each   "
                        f"use% range: {pct_str}   (uniform - use -e for per-target detail)"
                    )
                else:
                    for r in ost_rows:
                        _print_target_row(r)

            for r in other_rows:
                _print_target_row(r)

            for r in unparsed_rows:
                print(f"      [unparsed] {r['unparsed']}")

            for r in summary_rows:
                print(
                    f"      {'(filesystem total)':<26} {r['total']:>7} total  {r['used']:>7} used  "
                    f"{r['avail']:>7} avail  ({r['use_pct']:>3}% used)"
                )

    if extended:
        print("\n[ Extended: Raw Mount Details ]")
        for node in sorted(data.keys()):
            node_data = data[node]
            if "error" in node_data:
                continue
            print(f"\n--- {node} ---")
            for m in node_data.get("mounts", []):
                print(f"  {m['mountpoint']}:")
                for k, v in m.items():
                    print(f"    {k:<12}: {v}")
