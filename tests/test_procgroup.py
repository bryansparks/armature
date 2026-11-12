"""Signalling a process group by each member's pid, for a sandbox that refuses a negative pid."""
from __future__ import annotations

import logging
import os
import signal

import pytest

from armature import procgroup

LEADER, MEMBER, FORKED, STRANGER = 4_000_001, 4_000_002, 4_000_003, 4_000_004


def _proc(root, members):
    for pid, (comm, pgrp) in members.items():
        (root / str(pid)).mkdir(parents=True)
        (root / str(pid) / "stat").write_text(f"{pid} ({comm}) S 1 {pgrp} {pgrp} 0 -1 4194560\n")
    return root


def test_each_member_of_the_group_gets_a_signal_of_its_own(tmp_path, monkeypatch):
    proc = _proc(tmp_path / "proc", {LEADER: ("armature", LEADER), MEMBER: ("a) S 1 (b", LEADER),
                                     STRANGER: ("other", 7), os.getpid(): ("self", LEADER)})
    (proc / "self").mkdir()
    (proc / "4000005").mkdir()
    sent = []

    def kill(pid, sig):
        sent.append((pid, sig))
        if pid == LEADER:
            _proc(proc, {FORKED: ("forked", LEADER)})

    monkeypatch.setattr(procgroup.os, "kill", kill)
    monkeypatch.setattr(procgroup.os, "killpg", lambda *args: pytest.fail("a negative pid reaches nothing here"))
    assert procgroup.signal_group(LEADER, signal.SIGTERM, proc)
    assert sorted(sent) == [(LEADER, signal.SIGTERM), (MEMBER, signal.SIGTERM), (FORKED, signal.SIGTERM)]


def test_a_group_whose_members_have_exited_is_reached_by_nothing(tmp_path, monkeypatch):
    proc = _proc(tmp_path / "proc", {STRANGER: ("other", 7)})
    assert not procgroup.signal_group(LEADER, signal.SIGKILL, proc)
    _proc(proc, {LEADER: ("armature", LEADER)})

    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(procgroup.os, "kill", gone)
    assert not procgroup.signal_group(LEADER, signal.SIGKILL, proc)


def test_a_member_that_refuses_a_signal_is_logged_once(tmp_path, monkeypatch, caplog):
    proc = _proc(tmp_path / "proc", {LEADER: ("armature", LEADER)})

    def refuse(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(procgroup.os, "kill", refuse)
    monkeypatch.setattr(procgroup, "_refused", set())
    with caplog.at_level(logging.WARNING, logger=procgroup.__name__):
        assert not procgroup.signal_group(LEADER, signal.SIGTERM, proc)
        assert not procgroup.signal_group(LEADER, signal.SIGKILL, proc)
    assert [r.getMessage() for r in caplog.records] == [
        f"could not send signal {int(signal.SIGTERM)} to process {LEADER} of group {LEADER}: permission denied"]


@pytest.mark.parametrize("error", [None, ProcessLookupError, PermissionError])
def test_without_proc_it_is_killpg_and_an_exited_group_is_reached_by_nothing(tmp_path, monkeypatch, error):
    sent = []

    def killpg(pgid, sig):
        sent.append((pgid, sig))
        if error is not None:
            raise error

    monkeypatch.setattr(procgroup.os, "killpg", killpg)
    assert procgroup.members(LEADER, tmp_path / "nothing") is None
    assert procgroup.signal_group(LEADER, signal.SIGTERM, tmp_path / "nothing") is (error is None)
    assert sent == [(LEADER, signal.SIGTERM)]
