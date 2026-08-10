import os
import socket
import subprocess
from functools import lru_cache
from typing import Dict, Tuple, Union

DEFAULT_SSH_TIMEOUT = 3

# How much extra wall-clock time we give subprocess.run() on top of the SSH
# ConnectTimeout, to allow for the remote command itself to run.
DEFAULT_EXTRA_TIMEOUT = 2


@lru_cache(maxsize=1)
def _local_names() -> frozenset:
    """Every hostname spelling this machine might legitimately be called by
    a Slurm NodeName - short and FQDN forms of both gethostname() and
    getfqdn(), since the two don't always agree (e.g. DHCP-assigned FQDNs,
    /etc/hosts overrides). Cached: this doesn't change during a run, and it's
    looked up once per node in a thread pool, so the syscalls shouldn't repeat.
    """
    names = set()
    for fn in (socket.gethostname, socket.getfqdn):
        try:
            full = fn()
        except OSError:
            continue
        if not full:
            continue
        names.add(full.lower())
        names.add(full.split(".", 1)[0].lower())
    return frozenset(names)


def is_local_node(node: str) -> bool:
    """True if `node` (a Slurm NodeName) refers to the host this process is
    already running on.

    Set HPCPROBE_FORCE_SSH=1 to disable this and always go over SSH, e.g. to
    verify SSH trust to a node independently of the local short-circuit.
    """
    if os.environ.get("HPCPROBE_FORCE_SSH"):
        return False
    return node.strip().lower() in _local_names()


def local_run(
    command: str,
    timeout: int = DEFAULT_SSH_TIMEOUT,
    extra_timeout: int = DEFAULT_EXTRA_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run `command` directly on this host, no SSH involved.

    shell=True on POSIX execs via /bin/sh -c, the same shell OpenSSH hands
    the command string to on the remote end, so multi-line command bodies
    (see net.py's REMOTE_SCRIPT) behave identically either way.
    """
    return subprocess.run(
        command, shell=True, capture_output=True, text=True,
        timeout=timeout + extra_timeout,
    )


def ssh_run(
    node: str,
    command: str,
    timeout: int = DEFAULT_SSH_TIMEOUT,
    extra_timeout: int = DEFAULT_EXTRA_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run `command` on `node` with hpcprobe's standard batch SSH options.

    Raises subprocess.TimeoutExpired or OSError on failure - use ssh_poll()
    instead if you just want an {"error": ...} dict back.
    """
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "StrictHostKeyChecking=no",
        "-o", "LogLevel=QUIET",
        node,
        command,
    ]
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout + extra_timeout
    )


def ssh_poll(
    node: str,
    command: str,
    timeout: int = DEFAULT_SSH_TIMEOUT,
    extra_timeout: int = DEFAULT_EXTRA_TIMEOUT,
    fail_label: str = "ssh_command_failed",
) -> Union[Tuple[subprocess.CompletedProcess, None], Tuple[None, Dict[str, str]]]:
    """Run `command` on `node`, collapsing every failure mode into the same
    {"error": ...} shape the command modules already return today.

    If `node` is this host, the command runs directly via subprocess - no
    SSH, no key, no shell requirement on the account running hpcprobe. This is
    what lets a service account with a /sbin/nologin-style shell (or simply
    no SSH trust set up at all) still monitor the host it's already running
    on; it only needs SSH for genuinely remote nodes.

    Returns (result, None) on success, or (None, error_dict) on failure -
    callers do `result, err = ssh_poll(...); if err: return node, err`.
    """
    try:
        if is_local_node(node):
            result = local_run(command, timeout=timeout, extra_timeout=extra_timeout)
        else:
            result = ssh_run(node, command, timeout=timeout, extra_timeout=extra_timeout)
        if result.returncode != 0:
            return None, {"error": fail_label}
        return result, None
    except subprocess.TimeoutExpired:
        return None, {"error": "timeout"}
    except Exception as e:
        return None, {"error": str(e)}
