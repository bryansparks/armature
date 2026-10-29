"""Threads: which issue a run belongs to, from its subject and reported summary."""
import pytest

from armature.server.threads import joining, linked_pulls, links, thread_of

PULL = "fs-eng/android-tree#1877"
LINK = {"label": "Pull request", "url": "https://github.com/fs-eng/android-tree/pull/1877"}


def test_an_issue_subject_is_its_own_thread():
    assert thread_of("MEMTEMDIS-1475", None) == "MEMTEMDIS-1475"
    assert thread_of("MEMTEMDIS-1475", {"title": "[OTHER-2] ignored"}) == "MEMTEMDIS-1475"


def test_a_pull_request_joins_the_issue_its_title_starts_with():
    assert thread_of(PULL, {"title": "[MEMTEMDIS-1475] Fix the tree"}) == "MEMTEMDIS-1475"
    assert thread_of(PULL, {"title": "[MEM_2X-9]"}) == "MEM_2X-9"


@pytest.mark.parametrize("title", [None, "", "Fix the tree", "[memtemdis-1475] Fix", "[Memtemdis-1475] Fix",
                                   " [MEMTEMDIS-1475] Fix", "Fix [MEMTEMDIS-1475]", "MEMTEMDIS-1475 Fix",
                                   "[MEMTEMDIS-1475 Fix", "(MEMTEMDIS-1475) Fix", "[M-1] Fix", "[MEM-0] Fix",
                                   "[1MEM-2] Fix", 7])
def test_a_pull_request_without_a_bracketed_key_prefix_has_no_thread(title):
    assert thread_of(PULL, {"title": title}) is None


def test_a_key_too_long_for_the_column_is_no_thread():
    longest = "A" * 101 + "-1234567890"
    assert thread_of(longest, None) == longest and len(longest) <= 191
    assert thread_of("A" * 102 + "-1", None) is None
    assert thread_of(PULL, {"title": f"[{'A' * 250}-1] Long"}) is None


def test_joining_names_the_subjects_whose_runs_follow_a_run_into_its_thread():
    assert joining("MEM-1", {"links": [LINK]}) == ("MEM-1", [PULL])
    assert joining(PULL, {"title": "[MEM-1] x", "links": [LINK]}) == ("MEM-1", [PULL])
    assert joining(PULL, {"title": "x"}) == (None, [])


def test_other_subjects_have_no_thread():
    assert thread_of(None, {"title": "[MEM-1] x"}) is None
    assert thread_of("digest", {"title": "[MEM-1] x"}) is None
    assert thread_of("mem-1", None) is None
    assert thread_of(PULL, "not a dict") is None


def test_linked_pulls_reads_only_pull_request_links_to_github_pulls():
    summary = {"links": [LINK, {"label": "pull request ", "url": "https://github.com/a/b/pull/2/"},
                         {"label": "Jira", "url": "https://github.com/a/b/pull/3"},
                         {"label": "Pull request", "url": "https://github.com/a/b/issues/4"},
                         {"label": "Pull request", "url": "https://evil.example/a/b/pull/5"},
                         {"label": "Pull request", "url": "https://github.com/a/b/pull/6#discussion"},
                         {"label": "Pull request"}, "junk"]}
    assert linked_pulls(summary) == [PULL, "a/b#2"]
    assert linked_pulls({"links": "nope"}) == []
    assert linked_pulls(None) == []


def test_links_add_the_pull_request_a_pull_request_subject_names():
    review = {"label": "Review", "url": "https://github.com/fs-eng/android-tree/pull/1877#r1"}
    assert links(PULL, {"links": [review]}) == [LINK, review]
    assert links(PULL, {"links": [LINK, review]}) == [LINK, review]
    assert links(PULL, None) == [LINK]
    assert links("MEM-1", {"links": [review, "junk"]}) == [review]
    assert links(None, {"links": "nope"}) == []

