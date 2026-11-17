"""Signalling a process group: killpg first, and each member by its pid where a sandbox refuses a negative pid."""
from __future__ import annotations

import logging
import os
import signal

import pytest

from armature import procgroup

LEADER, MEMBER, FORKED, STRANGER = 4_000_001, 4_000_002, 4_000_003, 4_000_004


def _proc(root, members, own=None):
    root.mkdir(parents=True, exist_ok=True)
    if not (root / "self").is_symlink():
        (root / "self").symlink_to(str(own or os.getpid()))
    for pid, (comm, pgrp, *state) in members.items():
        (root / str(pid)).mkdir()
        (root / str(pid) / "stat").write_text(f"{pid} ({comm}) {state[0] if state else 'S'} 1 {pgrp} {pgrp} 0 -1\n")
    return root


def _refuse(*args):
    raise PermissionError(1, "Operation not permitted")


@pytest.mark.parametrize(("error", "reached"), [(None, True), (ProcessLookupError, False)])
def test_killpg_comes_first_and_only_its_eperm_walks_proc(tmp_path, monkeypatch, error, reached):
    sent = []

    def killpg(pgid, sig):
        sent.append((pgid, sig))
        if error is not None:
            raise error

    monkeypatch.setattr(procgroup.os, "killpg", killpg)
    monkeypatch.setattr(procgroup, "_listing", lambda *args: pytest.fail("a group killpg answered needs no walk"))
    assert procgroup.signal_group(LEADER, signal.SIGTERM, tmp_path) is reached
    assert sent == [(LEADER, signal.SIGTERM)]


def test_where_killpg_is_refused_each_member_gets_a_signal_of_its_own(tmp_path, monkeypatch):
    proc = _proc(tmp_path / "proc", {LEADER: ("armature", LEADER), MEMBER: ("a) S 1 (b", LEADER),
                                     STRANGER: ("other", 7), os.getpid(): ("self", LEADER)})
    (proc / "4000005").mkdir()
    sent = []

    def kill(pid, sig):
        sent.append((pid, sig))
        if pid == LEADER:
            _proc(proc, {FORKED: ("forked", LEADER)})

    monkeypatch.setattr(procgroup.os, "killpg", _refuse)
    monkeypatch.setattr(procgroup.os, "kill", kill)
    assert procgroup.signal_group(LEADER, signal.SIGTERM, proc)
    assert sorted(sent) == [(LEADER, signal.SIGTERM), (MEMBER, signal.SIGTERM), (FORKED, signal.SIGTERM)]


def test_a_refused_group_proc_cant_list_is_reached_by_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(procgroup.os, "killpg", _refuse)
    monkeypatch.setattr(procgroup.os, "kill", lambda *args: pytest.fail("nothing to signal"))
    assert not procgroup.signal_group(LEADER, signal.SIGKILL, tmp_path / "nothing")
    assert not procgroup.signal_group(LEADER, signal.SIGKILL, _proc(tmp_path / "proc", {STRANGER: ("other", 7)}))


def test_a_proc_from_another_pid_namespace_lists_nothing(tmp_path):
    proc = _proc(tmp_path / "proc", {LEADER: ("armature", LEADER)}, own=os.getpid() + 1)
    assert procgroup.members(LEADER, proc) is None
    assert procgroup.members(LEADER, tmp_path / "nothing") is None
    assert procgroup.members(LEADER, _proc(tmp_path / "ours", {LEADER: ("armature", LEADER)})) == [LEADER]


def test_a_proc_that_cant_be_read_cant_tell(tmp_path, monkeypatch):
    proc = _proc(tmp_path / "proc", {LEADER: ("armature", LEADER)})

    def unreadable(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(type(proc), "iterdir", unreadable)
    assert procgroup.members(LEADER, proc) is None


def test_a_live_member_that_refuses_a_signal_is_logged_once_and_a_zombie_never(tmp_path, monkeypatch, caplog):
    proc = _proc(tmp_path / "proc", {LEADER: ("armature", LEADER), MEMBER: ("gone", LEADER, "Z")})
    monkeypatch.setattr(procgroup.os, "killpg", _refuse)
    monkeypatch.setattr(procgroup.os, "kill", _refuse)
    monkeypatch.setattr(procgroup, "_refused", set())
    with caplog.at_level(logging.WARNING, logger=procgroup.__name__):
        assert not procgroup.signal_group(LEADER, signal.SIGTERM, proc)
        assert not procgroup.signal_group(LEADER, signal.SIGKILL, proc)
    assert [r.getMessage() for r in caplog.records] == [
        f"could not send signal {int(signal.SIGTERM)} to process {LEADER} of group {LEADER}: permission denied"]
