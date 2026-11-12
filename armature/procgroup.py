"""Signalling a whole process group, also inside a sandbox that refuses a negative pid.

Such a sandbox answers EPERM to ``killpg`` and to ``kill`` with a pid of 0 or below, so a group signalled that way
gets nothing. There, each member of the group that ``/proc`` lists is signalled by its own pid instead.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

PROC = Path("/proc")
# The most passes a signal makes: a fork that outruns this many is left to the caller's next signal.
PASSES = 10
ZOMBIE = "Z"

_refused: set[int] = set()


def _listing(pgid: int, proc: Path | None) -> dict[int, str] | None:
    """Each process in group ``pgid`` with its state, or None when ``proc`` can't tell."""
    proc = PROC if proc is None else proc
    try:
        # A /proc from another pid namespace names other processes by these pids.
        if os.readlink(proc / "self") != str(os.getpid()):
            return None
        entries = [entry for entry in proc.iterdir() if entry.name.isdigit()]
    except OSError:
        return None
    found = {}
    for entry in entries:
        try:
            stat = (entry / "stat").read_bytes()
        except OSError:
            continue
        # The command name in parentheses may hold spaces and parentheses itself.
        fields = stat[stat.rfind(b")") + 1:].split()
        if len(fields) > 2 and fields[2] == str(pgid).encode():
            found[int(entry.name)] = fields[0].decode("ascii", "replace")
    return found


def members(pgid: int, proc: Path | None = None) -> list[int] | None:
    """The pids in process group ``pgid``, or None when ``proc`` (default /proc) can't tell. A /proc that hides
    other users' processes lists none of theirs."""
    listed = _listing(pgid, proc)
    return None if listed is None else sorted(listed)


def signal_group(pgid: int, sig: int, proc: Path | None = None) -> bool:
    """Send ``sig`` to process group ``pgid``, and return whether it reached any process.

    ``killpg`` first. Only where it answers EPERM does each member ``proc`` lists get a kill of its own, but this
    process, with another pass for what a fork added during the last. A live member that refuses its signal there is
    logged once.
    """
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    listed = _listing(pgid, proc)
    seen, reached = {os.getpid()}, False
    for _ in range(PASSES):
        # macOS answers EPERM, not ESRCH, for a group whose members have all exited, and has no /proc.
        fresh = {pid: state for pid, state in (listed or {}).items() if pid not in seen}
        if not fresh:
            break
        for pid, state in fresh.items():
            seen.add(pid)
            try:
                os.kill(pid, sig)
                reached = True
            except ProcessLookupError:
                pass
            except PermissionError:
                if state != ZOMBIE and pid not in _refused:
                    _refused.add(pid)
                    log.warning("could not send signal %d to process %d of group %d: permission denied", sig, pid,
                                pgid)
        listed = _listing(pgid, proc)
    return reached
