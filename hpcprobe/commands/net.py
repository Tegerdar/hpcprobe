import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hpcprobe.core.cluster import poll_cluster
from hpcprobe.core.discovery import resolve_nodes
from hpcprobe.core.output import render_or_print
from hpcprobe.core.ssh import ssh_poll

# Counters pulled from `ethtool -S`. Kept to a deliberately small set: these are
# the ones that actually indicate a problem (link errors, buffer drops, pause
# frames) rather than the ~500+ per-queue counters ethtool -S also emits.
ETHTOOL_KEYS = (
    "rx_crc_errors_phy",
    "rx_symbol_err_phy",
    "link_down_events_phy",
    "rx_out_of_buffer",
    "rx_discards_phy",
    "tx_discards_phy",
    "rx_pause_ctrl_phy",
    "tx_pause_ctrl_phy",
    "rx_global_pause",
)

# All of the above are monotonic counters (since boot / since last driver
# reset), so all are safe to delta between two snapshots.
DELTA_KEYS = ETHTOOL_KEYS


def _snapshot_path() -> Path:
    """Where the last-run snapshot lives. Respects XDG_CACHE_HOME if set,
    otherwise falls back to ~/.cache/hpcprobe/ - no root required either way."""
    base = os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
    return Path(base) / "hpcprobe" / "net_snapshot.json"


def _load_snapshot() -> Optional[Dict[str, Any]]:
    path = _snapshot_path()
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _save_snapshot(cluster_state: Dict[str, Dict[str, Any]]) -> None:
    path = _snapshot_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump({"timestamp": time.time(), "data": cluster_state}, f)
    except OSError as e:
        print(f"Warning: could not write snapshot to {path}: {e}", file=sys.stderr)


def _compute_deltas(
    current: Dict[str, Dict[str, Any]], previous: Dict[str, Any]
) -> Tuple[Dict[str, Dict[str, Any]], float]:
    """Overlay a 'delta' block onto each netdev's stats, computed against the
    previous snapshot. Returns (annotated_current, elapsed_seconds).
    Counter resets (current < previous, e.g. after a driver reload or reboot)
    are reported as None rather than a misleading negative number.
    """
    prev_data = previous.get("data", {})
    elapsed = max(time.time() - previous.get("timestamp", time.time()), 0.001)

    for node, node_data in current.items():
        if "error" in node_data:
            continue
        prev_node = prev_data.get(node, {})
        if "error" in prev_node:
            continue
        prev_netdevs = prev_node.get("netdevs", {})

        for nd, ndata in node_data.get("netdevs", {}).items():
            stats = ndata.get("stats", {})
            prev_stats = prev_netdevs.get(nd, {}).get("stats", {})
            if not stats or not prev_stats:
                continue

            delta = {}
            for key in DELTA_KEYS:
                cur_val = stats.get(key)
                prev_val = prev_stats.get(key)
                if cur_val is None or prev_val is None:
                    continue
                try:
                    diff = int(cur_val) - int(prev_val)
                    delta[key] = diff if diff >= 0 else None  # None = counter reset
                except ValueError:
                    continue
            if delta:
                ndata["delta"] = delta

    return current, elapsed


# Remote-side collector. Root-free by design (matches the `cat /proc/meminfo`
# and `lscpu -J` pattern used by mem.py / cpu.py). Emits one delimited line per
# record so the local side can parse with a plain split('|') - no remote JSON
# dependency required (ethtool has no -J mode for -S output anyway).
#
# Three record types:
#   IBPORT|<ib_device>|<port>|<state>|<phys_state>|<link_layer>|<rate>|<netdev>
#   NETDEV|<netdev>|<operstate>|<carrier>|<speed>|<mtu>
#   ETHSTATS|<netdev>|<key>=<val>;<key>=<val>;...
REMOTE_SCRIPT = r"""
for ibdev in /sys/class/infiniband/*; do
  [ -d "$ibdev" ] || continue
  dev=$(basename "$ibdev")
  for portdir in "$ibdev"/ports/*; do
    [ -d "$portdir" ] || continue
    port=$(basename "$portdir")
    state=$(cat "$portdir/state" 2>/dev/null | awk '{print $2}')
    phys=$(cat "$portdir/phys_state" 2>/dev/null | awk '{print $2}')
    ll=$(cat "$portdir/link_layer" 2>/dev/null)
    rate=$(cat "$portdir/rate" 2>/dev/null)
    netdev="-"
    if [ -d "$ibdev/device/net" ]; then
      nd=$(ls "$ibdev/device/net" 2>/dev/null | head -n1)
      [ -n "$nd" ] && netdev="$nd"
    fi
    echo "IBPORT|${dev}|${port}|${state}|${phys}|${ll}|${rate}|${netdev}"
  done
done

for netdir in /sys/class/net/*; do
  nd=$(basename "$netdir")
  case "$nd" in lo|bonding_masters) continue ;; esac
  [ -d "$netdir/device" ] || continue
  operstate=$(cat "$netdir/operstate" 2>/dev/null)
  carrier=$(cat "$netdir/carrier" 2>/dev/null)
  speed=$(cat "$netdir/speed" 2>/dev/null)
  mtu=$(cat "$netdir/mtu" 2>/dev/null)
  echo "NETDEV|${nd}|${operstate}|${carrier}|${speed}|${mtu}"

  if command -v ethtool >/dev/null 2>&1; then
    stats=$(ethtool -S "$nd" 2>/dev/null | awk -F': ' '
      /rx_crc_errors_phy|rx_symbol_err_phy|link_down_events_phy|rx_out_of_buffer|rx_discards_phy|tx_discards_phy|rx_pause_ctrl_phy|tx_pause_ctrl_phy|rx_global_pause|rx_prio[0-9]_discards/ {
        gsub(/^[ \t]+|[ \t]+$/, "", $1); gsub(/^[ \t]+|[ \t]+$/, "", $2); printf "%s=%s;", $1, $2
      }')
    echo "ETHSTATS|${nd}|${stats}"
  fi
done
"""


def _parse_ethstats(raw: str) -> Dict[str, str]:
    """Parse 'key=val;key=val;' into a dict, skipping empty fragments."""
    out = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        out[k] = v
    return out


def _parse_remote_output(stdout: str) -> Dict[str, Any]:
    """Turn the delimited remote script output into a structured dict:
    {
        "ports": [ {device, port, state, phys_state, link_layer, rate, netdev}, ... ],
        "netdevs": { <netdev>: {operstate, carrier, speed, mtu, stats: {...}} }
    }
    """
    ports = []
    netdevs: Dict[str, Dict[str, Any]] = {}

    for line in stdout.strip().split("\n"):
        if not line or "|" not in line:
            continue
        fields = line.split("|")
        record = fields[0]

        if record == "IBPORT" and len(fields) == 8:
            _, dev, port, state, phys, ll, rate, netdev = fields
            ports.append({
                "device": dev,
                "port": port,
                "state": state or "UNKNOWN",
                "phys_state": phys or "UNKNOWN",
                "link_layer": ll or "-",
                "rate": rate or "-",
                "netdev": netdev,
            })

        elif record == "NETDEV" and len(fields) == 6:
            _, nd, operstate, carrier, speed, mtu = fields
            netdevs.setdefault(nd, {})
            netdevs[nd].update({
                "operstate": operstate or "unknown",
                "carrier": carrier or "-",
                "speed": speed or "-",
                "mtu": mtu or "-",
            })

        elif record == "ETHSTATS" and len(fields) == 3:
            _, nd, raw_stats = fields
            netdevs.setdefault(nd, {})
            netdevs[nd]["stats"] = _parse_ethstats(raw_stats)

    return {"ports": ports, "netdevs": netdevs}


def poll_node(node: str) -> Tuple[str, Dict[str, Any]]:
    """Fetch InfiniBand/RoCE port state and NIC error counters via SSH.

    Root-free: everything is read from sysfs plus `ethtool -S` (which does not
    require elevated privileges, unlike `ethtool -s` / config changes).
    """
    result, err = ssh_poll(node, REMOTE_SCRIPT, fail_label="ssh_auth_or_network_query_failed")
    if err:
        return node, err
    return node, _parse_remote_output(result.stdout)


def execute(args: Any) -> int:
    """Main execution router for the net subcommand."""
    target_nodes = resolve_nodes(args)
    if not target_nodes:
        print("No targets identified. Exiting.", file=sys.stderr)
        return 1

    extended = getattr(args, "extended", False)
    delta_mode = getattr(args, "delta", False)
    cluster_state = poll_cluster(target_nodes, poll_node)

    elapsed_seconds = None
    if delta_mode:
        previous = _load_snapshot()
        if previous is None:
            print(
                "No prior snapshot found - this run establishes the baseline. "
                "Run again later to see deltas.",
                file=sys.stderr,
            )
        else:
            cluster_state, elapsed_seconds = _compute_deltas(cluster_state, previous)
        _save_snapshot(cluster_state)

    render_or_print(
        args, cluster_state, "net", print_console,
        extended, delta_mode, elapsed_seconds,
    )
    return 0


def _fmt_rate(rate: str) -> str:
    """'100 Gb/sec (4X EDR)' -> '100G'; falls back to raw value."""
    if not rate or rate == "-":
        return "-"
    try:
        num = rate.split()[0]
        return f"{num}G"
    except (IndexError, ValueError):
        return rate


def print_console(
    data: Dict[str, Dict[str, Any]],
    extended: bool = False,
    delta_mode: bool = False,
    elapsed_seconds: Optional[float] = None,
) -> None:
    """Formats the network data into a clean terminal table."""
    if delta_mode and elapsed_seconds is not None:
        mins = elapsed_seconds / 60
        print(f"[ Delta mode: comparing against snapshot from {mins:.1f} min ago ]")

    width = 145 if delta_mode else 130
    print("=" * width)
    header = (
        f"{'Node':<12} | {'Device':<8} | {'Netdev':<12} | {'Link':<8} | {'Phys':<10} | "
        f"{'Layer':<10} | {'Rate':<6} | {'OutOfBuf':<9} | {'CRC/Sym':<8} | {'PauseAct'}"
    )
    if delta_mode:
        header += f" | {'ΔOutOfBuf':<10} | {'ΔCRC/Sym'}"
    print(header)
    print("=" * width)

    for node in sorted(data.keys()):
        node_data = data[node]

        if "error" in node_data:
            print(f"{node:<12} | [ ERROR: {node_data['error']} ]")
            continue

        ports = node_data.get("ports", [])
        netdevs = node_data.get("netdevs", {})

        if not ports:
            print(f"{node:<12} | [ no InfiniBand/RoCE devices found ]")
            continue

        for p in ports:
            nd = p["netdev"]
            ndata = netdevs.get(nd, {}) if nd != "-" else {}
            stats = ndata.get("stats", {})
            delta = ndata.get("delta", {})

            oob = stats.get("rx_out_of_buffer", "-")
            crc = stats.get("rx_crc_errors_phy", "-")
            sym = stats.get("rx_symbol_err_phy", "-")
            crc_sym = f"{crc}/{sym}" if (crc != "-" or sym != "-") else "-"

            pause_rx = stats.get("rx_pause_ctrl_phy", "0")
            pause_tx = stats.get("tx_pause_ctrl_phy", "0")
            pause_active = "-"
            if nd != "-":
                try:
                    pause_active = "Yes" if (int(pause_rx) > 0 or int(pause_tx) > 0) else "No"
                except ValueError:
                    pause_active = "-"

            line = (
                f"{node:<12} | {p['device']:<8} | {nd:<12} | {p['state']:<8} | {p['phys_state']:<10} | "
                f"{p['link_layer']:<10} | {_fmt_rate(p['rate']):<6} | {oob:<9} | {crc_sym:<8} | {pause_active}"
            )

            if delta_mode:
                d_oob = delta.get("rx_out_of_buffer")
                d_crc = delta.get("rx_crc_errors_phy")
                d_sym = delta.get("rx_symbol_err_phy")
                d_oob_s = "reset" if d_oob is None and "rx_out_of_buffer" in delta else (str(d_oob) if d_oob is not None else "-")
                if d_crc is None and d_sym is None and not delta:
                    d_crc_sym = "-"
                else:
                    d_crc_s = "reset" if (d_crc is None and "rx_crc_errors_phy" in delta) else ("-" if d_crc is None else str(d_crc))
                    d_sym_s = "reset" if (d_sym is None and "rx_symbol_err_phy" in delta) else ("-" if d_sym is None else str(d_sym))
                    d_crc_sym = f"{d_crc_s}/{d_sym_s}"
                line += f" | {d_oob_s:<10} | {d_crc_sym}"

                # Flag anything that moved since last snapshot - this is the
                # whole point of delta mode, so make it visible, not buried.
                if (isinstance(d_oob, int) and d_oob > 0) or \
                   (isinstance(d_crc, int) and d_crc > 0) or \
                   (isinstance(d_sym, int) and d_sym > 0):
                    line += "  <-- CHANGED"

            print(line)

    print("=" * width)

    if extended:
        print("\n[ Extended Parameters ]")
        for node in sorted(data.keys()):
            node_data = data[node]
            if "error" in node_data:
                continue
            print(f"\n--- {node} ---")
            for nd, ndata in sorted(node_data.get("netdevs", {}).items()):
                print(f"  {nd}:")
                print(f"    operstate : {ndata.get('operstate', '-')}")
                print(f"    carrier   : {ndata.get('carrier', '-')}")
                print(f"    speed     : {ndata.get('speed', '-')}")
                print(f"    mtu       : {ndata.get('mtu', '-')}")
                for k, v in sorted(ndata.get("stats", {}).items()):
                    print(f"    {k:<28}: {v}")
