import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from fx_common import *

D = __file__.rsplit("/", 2)[0] + "/code-review"
REPO = "state/repos/fs-eng_android-tree"
HEAD = f"{{{{rev:{REPO}:MEMTEMDIS-4512-memories-cache}}}}"
BASE = f"{{{{rev:{REPO}:dev}}}}"

def pr_view(title="MEMTEMDIS-4512 Cache memories per person", body="Caches each person's memories so the tab opens instantly.\n\nFixes MEMTEMDIS-4512."):
    return {"state": "OPEN", "isDraft": False, "isCrossRepository": False, "headRefOid": HEAD, "baseRefName": "dev",
            "author": {"login": "jstutz4"}, "title": title, "body": body, "headRefName": "MEMTEMDIS-4512-memories-cache",
            "mergeCommit": None}

def fixture(desc, fakes, stubs=None, extra_files=None):
    files = {"config/teams.json": teams(), "config/roles.json": roles(),
             "state/pr-queue/job.json": {"repo": "fs-eng/android-tree", "number": 4821, "platform": "android",
                                         "mode": "review", "action": "opened", "head": HEAD}}
    files["config/pr-authors.json"] = {"authors": {"fs-eng/android-tree": ["jstutz4"]}}
    files["config/validate.json"] = {"commands": {"fs-eng/android-tree": "./validate.sh --quick --base {base}"}}
    files.update(extra_files or {})
    return {"description": desc, "source": "github", "env": {}, "files": files,
            "repos": {REPO: {"base": "dev", "branch": "MEMTEMDIS-4512-memories-cache", "remote": "fs-eng/android-tree",
                             "files": {**KOTLIN_BASE, "validate.sh": "#!/bin/sh\necho 'BUILD SUCCESSFUL in 41s'\n"},
                             "commits": [{"message": "Cache memories per person", "files": KOTLIN_CHANGE}]}},
            "context": {"job_file": "{{home}}/state/pr-queue/job.json"},
            "fakes": fakes, "stubs": stubs or {}}

VM = "app/src/main/java/org/familysearch/tree/memories/MemoriesViewModel.kt"
LEAD_BLOCK = {"verdict": "comment", "summary": "One blocking issue: the memories cache outlives sign-out.",
  "decisions": [{"claim": "The cache is never cleared on sign-out, so the next user sees the previous user's memories.",
                 "raised_by": ["correctness", "security"], "decision": "block", "reason": "Confirmed: nothing calls signedOut().",
                 "disposition": "keep"},
                {"claim": "signedOut() has no test.", "raised_by": ["test_adequacy"], "decision": "suggest",
                 "reason": "Worth a test once the call is wired.", "disposition": "keep"}],
  "comments": [{"path": VM, "line": 7, "severity": "high", "blocking": True, "claim": "Cache survives sign-out",
                "raised_by": ["correctness", "security"],
                "body": "This cache is keyed by person id only and nothing calls `signedOut()`, so after a sign-out the next user sees the previous user's memories. Clear it from the sign-out flow.",
                "verified": "Searched for signedOut callers: none."},
               {"path": VM, "line": 9, "severity": "low", "blocking": False, "claim": "No test for signedOut",
                "raised_by": ["test_adequacy"], "body": "Add a test that `signedOut()` empties the cache."}]}
REVIEWS = {"correctness": "1. MemoriesViewModel.kt:7 (high): the cache is never cleared on sign-out; nothing calls signedOut().",
           "security": "1. MemoriesViewModel.kt:7 (high): one user's memories can show to the next user on a shared device.",
           "test_adequacy": "1. MemoriesViewModel.kt:9 (low): signedOut() is untested."}
base_fakes = {
  "gh": [{"match": "^api user --jq .login", "stdout": "fs-armature\n"},
         {"match": "^pr view 4821 --repo fs-eng/android-tree --json", "stdout": pr_view()},
         {"match": "^api repos/fs-eng/android-tree/pulls/4821/requested_reviewers", "stdout": ""},
         {"match": "^api repos/fs-eng/android-tree/pulls/4821/files --paginate --slurp", "stdout": [[
             {"filename": VM, "status": "modified", "additions": 8, "deletions": 1,
              "patch": "@@ -1,5 +1,12 @@\n package org.familysearch.tree.memories\n \n class MemoriesViewModel(private val repo: MemoriesRepository) {\n-    fun load(personId: String) = repo.memories(personId)\n+    private var cache: Map<String, List<Memory>> = emptyMap()\n+\n+    fun load(personId: String): List<Memory> =\n+        cache[personId] ?: repo.memories(personId).also { cache = cache + (personId to it) }\n+\n+    fun signedOut() {\n+        cache = emptyMap()\n+    }\n }"}]]},
         {"match": "^api repos/fs-eng/android-tree/pulls/4821 --jq .state", "stdout": "open\n"},
         {"match": "^api repos/fs-eng/android-tree/pulls/4821 --jq .head.sha", "stdout": HEAD + "\n"},
         {"match": "^api repos/fs-eng/android-tree/pulls/4821 --jq .merged", "stdout": "false\n"},
         {"match": "^api repos/fs-eng/android-tree/pulls/4821/reviews --method POST|^api repos/fs-eng/android-tree/pulls/4821/reviews -X POST|^api -X POST repos/fs-eng/android-tree/pulls/4821/reviews|^api repos/fs-eng/android-tree/pulls/4821/reviews --input",
          "stdout": {"id": 2150001, "html_url": "https://github.com/fs-eng/android-tree/pull/4821#pullrequestreview-2150001", "state": "COMMENTED"}},
         {"match": "^api repos/fs-eng/android-tree/issues/comments/3150001 --method PATCH", "stdout": {"id": 3150001}},
         {"match": "--paginate --slurp", "stdout": [[]]},
         {"match": "^api repos/fs-eng/android-tree/issues/4821/comments -f body=", "stdout": {"id": 3150001, "html_url": "https://github.com/fs-eng/android-tree/pull/4821#issuecomment-3150001"}}],
  "claude": [{"match": "--json-schema", "result": {"result": "", "structured_output": LEAD_BLOCK}}]
            + [{"match": f"\\n\\n[^\\n]*{role}|prompts/{role}\\.md", "result": text} for role, text in REVIEWS.items()]
            + [{"match": "", "result": "No findings."}],
}
write(f"{D}/blocking-finding.json", fixture("A new pull request with one confirmed high finding: the lead blocks and posts it inline.", base_fakes))

import copy

LEAD_APPROVE = {"verdict": "approve", "summary": "Clean change: the cache is local and cleared on sign-out.", "decisions": [],
                "comments": []}
approve = copy.deepcopy(base_fakes)
approve["claude"] = [{"match": "--json-schema", "result": {"result": "", "structured_output": LEAD_APPROVE}},
                     {"match": "", "result": "No findings."}]
approve["gh"] = [g for g in approve["gh"] if "reviews --method POST" not in g["match"]] + []
approve["gh"].insert(0, {"match": "^api repos/fs-eng/android-tree/pulls/4821/reviews --method POST",
                         "stdout": {"id": 2150002, "html_url": "https://github.com/fs-eng/android-tree/pull/4821#pullrequestreview-2150002", "state": "APPROVED"}})
approve["gh"] = [dict(g, stdout={"id": 3150001}) if "PATCH" in g["match"] else g for g in approve["gh"]]
write(f"{D}/approve-clean.json", fixture("Every role finds nothing and the build passes, so the lead approves with no inline comments.", approve))

outsider = copy.deepcopy(base_fakes)
outsider["gh"][1] = {"match": "^pr view 4821 --repo fs-eng/android-tree --json",
                     "stdout": {**pr_view(), "author": {"login": "contractor-dev"}}}
write(f"{D}/author-not-allowed.json", fixture("An author outside pr-authors.json gets no review: resolve declines and nothing posts.", outsider))
