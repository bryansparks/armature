import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from armature.events import location, register
from armature.events.emitter import is_must_keep
from armature.events.summary import (
    link_url,
    merge_summary,
    subject_problem,
    summary_patch,
    trigger_ref_problem,
)
from tests.events.support import DatagramReader

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("subject", ["fs-eng/android-tree#123", "MEMTEMDIS-886", "daily-review", "a", "x" * 191])
def test_good_subjects(subject):
    assert subject_problem(subject) is None


@pytest.mark.parametrize("subject", ["", "x" * 192, "a b", "a;b", "<x>", None, 5, "repo#1\n"])
def test_bad_subjects(subject):
    assert subject_problem(subject)


def test_trigger_ref_limits():
    assert trigger_ref_problem({"repo": "r", "number": 1, "ok": True, "f": 1.5, "job": {"mode": "fix"}}) is None
    assert trigger_ref_problem({"a": {"b": {"c": {"d": 1}}}}) is None
    assert trigger_ref_problem({"a": {"b": {"c": {"d": {"e": 1}}}}})
    assert trigger_ref_problem([1])
    assert trigger_ref_problem({"a": [1]})
    assert trigger_ref_problem({"a": None})
    assert trigger_ref_problem({"a": float("nan")})
    assert trigger_ref_problem({"a": "x" * 4096})
    assert trigger_ref_problem({"a": "x" * 4000}) is None


@pytest.mark.parametrize("url,ok", [
    ("https://github.com/fs-eng/android-tree/pull/1", True),
    ("/runs/abc", True),
    ("http://github.com/x", False),
    ("//evil.example.com/x", False),
    ("javascript:alert(1)", False),
    ("https:///nohost", False),
    ("https://a.example.com/x y", False),
    ("/\\evil.example.com", False),
    ("data:text/html,x", False),
])
def test_link_urls(url, ok):
    assert (link_url(url) is not None) is ok


def test_patch_keeps_only_known_and_valid_parts():
    patch = summary_patch({"title": "  Fix\n the  crash ", "verdict": "x" * 100, "other": 1,
                           "links": [{"label": "PR", "url": "https://x.example.com/1"}, {"url": "https://y.example.com"},
                                     "nope", {"label": "Bad", "url": "ftp://x"}],
                           "fields": {"platform": "android", "bad key": "v", "n": 3, "list": [1]}})
    assert patch == {"title": "Fix the crash", "verdict": "x" * 64,
                     "links": [{"label": "PR", "url": "https://x.example.com/1"}],
                     "fields": {"platform": "android", "n": "3"}}
    assert summary_patch({"title": "", "verdict": True, "links": "x", "fields": []}) == {}


def test_patch_takes_a_known_outcome_and_never_a_risk():
    assert summary_patch({"outcome": "pr_opened", "risk": "low"}) == {"outcome": "pr_opened"}
    assert summary_patch({"outcome": "shipped"}) == {}
    assert summary_patch({"outcome": ["noop"]}) == {}
    assert merge_summary({"outcome": "recommended"}, {"outcome": "pushed"}) == {"outcome": "pushed"}


def test_merge_later_wins_and_links_dedupe_by_url():
    first = merge_summary(None, {"title": "A", "links": [{"label": "PR", "url": "https://x/1"}], "fields": {"a": "1"}})
    second = merge_summary(first, {"verdict": "approve", "fields": {"a": "2", "b": "3"},
                                   "links": [{"label": "Pull request", "url": "https://x/1"},
                                             {"label": "Review", "url": "https://x/2"}]})
    assert second == {"title": "A", "verdict": "approve",
                      "links": [{"label": "Pull request", "url": "https://x/1"}, {"label": "Review", "url": "https://x/2"}],
                      "fields": {"a": "2", "b": "3"}}
    assert merge_summary({"post_url": "https://x/9", "title": "Old"}, {"title": "New"}) == \
        {"post_url": "https://x/9", "title": "New"}
    assert first == {"title": "A", "links": [{"label": "PR", "url": "https://x/1"}], "fields": {"a": "1"}}


def test_merge_caps_links():
    current = {"links": [{"label": str(i), "url": f"https://x/{i}"} for i in range(20)]}
    merged = merge_summary(current, {"links": [{"label": "new", "url": "https://x/new"}]})
    assert len(merged["links"]) == 20 and merged["links"][-1]["url"] == "https://x/new"


def test_summary_is_must_keep():
    assert is_must_keep("summary")


def test_registration_body_reads_subject_and_trigger_ref(monkeypatch):
    monkeypatch.setenv("ARMATURE_SUBJECT", "fs-eng/android-tree#5")
    monkeypatch.setenv("ARMATURE_TRIGGER_REF", json.dumps({"repo": "fs-eng/android-tree", "number": 5}))
    body = register.registration_body("w", None)
    assert body["subject"] == "fs-eng/android-tree#5"
    assert body["trigger_ref"] == {"repo": "fs-eng/android-tree", "number": 5}


@pytest.mark.parametrize("subject,ref", [("bad subject", "not json"), ("a;b", "[1]"), ("x" * 200, '{"a": null}')])
def test_registration_body_drops_bad_values(monkeypatch, subject, ref):
    monkeypatch.setenv("ARMATURE_SUBJECT", subject)
    monkeypatch.setenv("ARMATURE_TRIGGER_REF", ref)
    body = register.registration_body("w", None)
    assert body["subject"] is None and body["trigger_ref"] == {}


def test_registration_body_names_this_machine_unless_armature_location_says_otherwise(monkeypatch):
    monkeypatch.setattr(location, "_chip", lambda: "Apple M1")
    monkeypatch.delenv("ARMATURE_LOCATION", raising=False)
    assert register.registration_body("w", None)["location"]["label"] == "M1 (local)"
    monkeypatch.setenv("ARMATURE_LOCATION", '{"kind": "aws-linux", "instance": "i-0abc"}')
    assert register.registration_body("w", None)["location"] == {"kind": "aws-linux", "label": "AWS Linux",
                                                                 "instance": "i-0abc"}
    for bad in ("not json", '{"kind": "gcp"}'):
        monkeypatch.setenv("ARMATURE_LOCATION", bad)
        assert register.registration_body("w", None)["location"]["kind"] == "local"


def test_registration_body_without_env(monkeypatch):
    monkeypatch.delenv("ARMATURE_SUBJECT", raising=False)
    monkeypatch.delenv("ARMATURE_TRIGGER_REF", raising=False)
    body = register.registration_body("w", None)
    assert body["subject"] is None and body["trigger_ref"] == {}


def _cli(args, env):
    return subprocess.run([sys.executable, "-m", "armature.events", *args], capture_output=True, cwd=REPO, env=env,
                          timeout=60, check=False)


def test_cli_summary_builds_the_payload():
    reader = DatagramReader()
    try:
        env = {**os.environ, "ARMATURE_EVENTS": reader.endpoint, "ARMATURE_EVENTS_TOKEN": "t"}
        proc = _cli(["summary", "--field", "title=123", "--field", "verdict=approve", "--field", "platform=ios",
                     "--link", "Pull request=https://github.com/o/r/pull/1", "--link", "Run=/runs/x"], env)
        assert proc.returncode == 0, proc.stderr
        event = reader.wait_for(1)[0]
        assert event["kind"] == "summary"
        assert event["payload"] == {"title": "123", "verdict": "approve", "fields": {"platform": "ios"},
                                    "links": [{"label": "Pull request", "url": "https://github.com/o/r/pull/1"},
                                              {"label": "Run", "url": "/runs/x"}]}
        assert _cli(["summary", "--link", "Bad=http://x.example.com"], env).returncode != 0
        assert _cli(["summary", "--link", "nolabel"], env).returncode != 0
        assert _cli(["log", "--link", "A=https://x.example.com"], env).returncode != 0
    finally:
        reader.close()
