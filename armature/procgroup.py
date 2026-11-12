"""Signalling a whole process group, also inside a sandbox that refuses a negative pid.

Such a sandbox answers EPERM to ``killpg`` and to ``kill`` with a pid of 0 or below, so a group signalled that way
gets nothing. Where ``/proc`` lists the processes, each member of the group is signalled by its own pid instead.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

PROC = Path("/proc")
# The most passes a signal makes: a fork that outruns this many is left to the caller's next signal.
PASSES = 10

_refused: set[int] = set()


def members(pgid: int, proc: Path | None = None) -> list[int] | None:
    """The pids in process group ``pgid``, or None where there is no ``proc`` (default /proc) to list them."""
    proc = PROC if proc is None else proc
    if not proc.is_dir():
        return None
    try:
        entries = [entry for entry in proc.iterdir() if entry.name.isdigit()]
    except OSError:
        return []
    found = []
    for entry in entries:
        try:
            stat = (entry / "stat").read_bytes()
        except OSError:
            continue
        # The command name in parentheses may hold spaces and parentheses itself.
        fields = stat[stat.rfind(b")") + 1:].split()
        if len(fields) > 2 and fields[2] == str(pgid).encode():
            found.append(int(entry.name))
    return found


def signal_group(pgid: int, sig: int, proc: Path | None = None) -> bool:
    """Send ``sig`` to every process in group ``pgid`` but this one, and return whether it reached any.

    With ``proc`` each member gets a kill of its own, and another pass reaches what a fork added during the last. A
    member that refuses its signal is logged once. Without it, this is ``killpg``.
    """
    pids = members(pgid, proc)
    if pids is None:
        try:
            os.killpg(pgid, sig)
        # macOS answers EPERM, not ESRCH, for a group whose members have all exited.
        except (ProcessLookupError, PermissionError):
            return False
        return True
    seen, reached = {os.getpid()}, False
    for _ in range(PASSES):
        fresh = [pid for pid in pids if pid not in seen]
        if not fresh:
            break
        for pid in fresh:
            seen.add(pid)
            try:
                os.kill(pid, sig)
                reached = True
            except ProcessLookupError:
                pass
            except PermissionError:
                if pid not in _refused:
                    _refused.add(pid)
                    log.warning("could not send signal %d to process %d of group %d: permission denied", sig, pid,
                                pgid)
        pids = members(pgid, proc) or []
    return reached
