import difflib
import sys
from pathlib import Path

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from fx_common import *

D = __file__.rsplit("/", 2)[0] + "/development"
SLACK_TS = "1700000000.000100"
OWNER = "U04F2KQ8V3M"
ME = "557058:4f1c2d3e-8a9b-4c7d-9e1f-2a3b4c5d6e7f"
OTHER = "557058:9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a"
SPRINT = [{"id": 18422, "name": "MEMTEMDIS Sprint 2026.22", "state": "active", "boardId": 1312,
           "startDate": "2026-10-21T15:00:00.000Z", "endDate": "2026-11-04T15:00:00.000Z"}]

# The developer checkout is the shared clone, so development_resolve.sh's local `git fetch "$checkout"`,
# which the capture fakes like any fetch, has nothing to bring over.
AND = "state/repos/fs-eng_android-tree"
IOS = "state/repos/fs-eng_mobile-ios"


def patch(path, before, after):
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{path}", f"b/{path}"))


def dev_config(ios_lint_fix=None):
    data = neutral(json.loads(Path(f"{OPS}/config/development.json").read_text()))
    data["platforms"].pop("dotnet", None)
    data["platforms"].pop("backend", None)
    data["platforms"]["android"]["checkout"] = "{{home}}/" + AND
    data["platforms"]["ios"]["checkout"] = "{{home}}/" + IOS
    data["deny_tools"] = [t.replace("/srv/armature-ops", "{{home}}")
                          for t in data["deny_tools"]]
    if ios_lint_fix:
        data["platforms"]["ios"]["lint"]["fix"] = ios_lint_fix
    return data


def jira_issue(key, summary, description, components, labels, assignee=ME, issuetype="Story"):
    return {"id": "1204587", "key": key, "self": "https://familysearch.atlassian.net/rest/api/2/issue/1204587",
            "fields": {"summary": summary, "description": description,
                       "issuetype": {"id": "10001", "name": issuetype, "subtask": False},
                       "status": {"name": "To Do", "statusCategory": {"id": 2, "key": "new", "name": "To Do"}},
                       "assignee": {"accountId": assignee, "displayName": "Stephan Heilner", "active": True},
                       "components": [{"id": "21304", "name": c} for c in components],
                       "labels": labels, "customfield_10020": SPRINT}}


def jira_curl(issue):
    key = issue["key"]
    return [{"match": "rest/api/3/myself", "stdout": {"accountId": ME, "displayName": "Stephan Heilner",
                                                       "emailAddress": "stephanh@familysearch.org", "active": True,
                                                       "timeZone": "America/Denver"}},
            {"match": f"/2/issue/{key}\\?fields=", "stdout": issue}]


def base_files(job, dev):
    return {"config/teams.json": teams(), "config/roles.json": roles(), "config/development.json": dev,
            "config/validate.json": {"commands": {"fs-eng/android-tree": "./validate.sh --quick --base {base}",
                                                  "fs-eng/mobile-ios": "./validate.sh --quick --base {base}"}},
            "config/workers.json": {"region": "us-east-1", "stack": "armature-workers", "session": {}},
            ".claude.json": {"mcpServers": {"jira": {"command": "uvx", "args": ["mcp-atlassian"],
                                                      "env": {"JIRA_URL": "https://familysearch.atlassian.net"}}}},
            "state/jira-queue/job.json": job}


ENV = {"SLACK_OWNER_USER": OWNER, "DEVELOPMENT_REVIEW_FAN_OUT": "1",
       "GIT_AUTHOR_DATE": "2026-10-28T09:30:00", "GIT_COMMITTER_DATE": "2026-10-28T09:30:00"}
GIT_NEW_BRANCH = [{"match": "ls-remote --exit-code --heads origin", "exit": 2}]

CONSULT = {
    "simplicity": "The cache invalidation this issue asks for already exists: MemoriesViewModel.memoryDeleted(id) on dev drops the person's cached list. Reuse it. Nothing new is needed unless the delete path does not call it.",
    "correctness": "memoryDeleted(personId) must run only after the repository delete succeeds, or a failed delete hides a memory that still exists. Confirm the caller orders it that way before changing anything.",
    "security": "No security concern: the change only drops cached memories for the signed-in user's own person id.",
    "performance": "Dropping one person's entry is O(1) on the Map. Do not clear the whole cache on delete.",
    "beauty": "No UI change is needed. The list re-renders from the ViewModel's state once the cache entry is gone.",
    "test_adequacy": "MemoriesViewModelTest.memoryDeleted_dropsCachedList on dev already covers this. If anything changes, extend that test rather than adding a new class.",
    "concurrency": "memoryDeleted runs on the main dispatcher, as load does, so the Map swap is not racing a load.",
    "conventions": "Follow MemoriesViewModel's existing pattern of replacing the immutable Map rather than switching to a mutable one.",
    "compatibility": "No API or stored-data change, so older app versions are unaffected.",
}
# How each prompts/<role>.md opens, which is how a fake tells the role sessions apart.
ROLE_LEAD = {"simplicity": "simplicity", "correctness": "correctness", "security": "security",
             "performance": "performance", "beauty": "visual and interaction design",
             "test_adequacy": "whether the tests actually verify", "concurrency": "concurrency",
             "conventions": "conventions", "compatibility": "backward and forward compatibility"}
BRIEF_ANDROID = {
    "feasible": True, "reason": "",
    "anchor": "A deleted memory disappears from the person's Memories tab without a restart, which needs a change in MemoriesViewModel's cache.",
    "brief": "- Reuse: `MemoriesViewModel.memoryDeleted(personId)` in app/src/main/java/org/familysearch/tree/memories/MemoriesViewModel.kt already drops the cached list. `MemoriesViewModelTest.memoryDeleted_dropsCachedList` already covers it.\n"
             "- Where: `MemoriesViewModel.kt`, and the delete success path that should call `memoryDeleted`.\n"
             "- Constraints: drop only the one person's entry (performance); call it after the delete succeeds (correctness); extend the existing test, not a new class (test_adequacy).\n"
             "- Decided: no UI change (beauty), and simplicity's point stands: if dev already does this, report it as already fixed.\n"
             "- Out of scope: the cache surviving sign-out, which MEMTEMDIS-4512 tracks."}
IMPLEMENT_ALREADY = ("I read MEMTEMDIS-4630 and traced the delete path. On dev, MEMTEMDIS-4611 (merged two days ago) added "
                     "`MemoriesViewModel.memoryDeleted(personId)`, which drops the person's cached list, with "
                     "`MemoriesViewModelTest.memoryDeleted_dropsCachedList` covering it. The reported build predates that merge. I ran `./gradlew :app:testDebugUnitTest --tests "
                     "'*MemoriesViewModelTest*'` and it passes, including memoryDeleted_dropsCachedList.\n\n"
                     + json.dumps({"outcome": "already_fixed",
                                   "summary": "MEMTEMDIS-4611 already invalidates the person's cached memories on delete, so the tab no longer shows a deleted memory.",
                                   "verification": "./gradlew :app:testDebugUnitTest --tests '*MemoriesViewModelTest*': 14 tests, 0 failures.",
                                   "pr_title": "", "pr_body": "",
                                   "follow_ups": ["The memories cache still survives sign-out (MEMTEMDIS-4512)."]}))

# -------------------------------------------------------- android, full run
AND_FIXED = {
    "app/src/main/java/org/familysearch/tree/memories/MemoriesViewModel.kt":
        "package org.familysearch.tree.memories\n\nclass MemoriesViewModel(private val repo: MemoriesRepository) {\n"
        "    private var cache: Map<String, List<Memory>> = emptyMap()\n\n"
        "    fun load(personId: String): List<Memory> =\n"
        "        cache[personId] ?: repo.memories(personId).also { cache = cache + (personId to it) }\n\n"
        "    fun memoryDeleted(personId: String) {\n        cache = cache - personId\n    }\n}\n",
    "app/src/test/java/org/familysearch/tree/memories/MemoriesViewModelTest.kt":
        "package org.familysearch.tree.memories\n\nimport org.junit.Assert.assertEquals\nimport org.junit.Test\n\n"
        "class MemoriesViewModelTest {\n    @Test\n    fun memoryDeleted_dropsCachedList() {\n"
        "        val repo = FakeMemoriesRepository(mutableListOf(Memory(\"m1\"), Memory(\"m2\")))\n"
        "        val viewModel = MemoriesViewModel(repo)\n        viewModel.load(\"KWCB-HZ2\")\n"
        "        repo.items.removeAt(0)\n        viewModel.memoryDeleted(\"KWCB-HZ2\")\n"
        "        assertEquals(listOf(Memory(\"m2\")), viewModel.load(\"KWCB-HZ2\"))\n    }\n}\n",
}
and_issue = jira_issue(
    "MEMTEMDIS-4630", "Memories tab still shows a memory after it is deleted",
    "Steps to reproduce:\n1. Open a person with several memories.\n2. Open a memory and delete it.\n3. Go back to the person's Memories tab.\n\n"
    "Expected: the deleted memory is gone.\nActual: it stays in the list until the app is restarted.\n\n"
    "Seen on Android 8.41.0 (build 84100312), Pixel 8, Android 15.",
    ["Android"], ["memories", "regression"], issuetype="Bug")
and_fakes = {
    "curl": jira_curl(and_issue),
    "git": GIT_NEW_BRANCH,
    "claude": [{"match": "You are implementing one Jira issue", "result": IMPLEMENT_ALREADY}]
              + [{"match": f"Your role is {ROLE_LEAD[role]}", "result": text} for role, text in CONSULT.items()],
}
write(f"{D}/android-already-fixed.json", {
    "description": "An Android bug newly assigned to me runs end to end: prepare reads it from Jira, PlatformTriage picks "
                   "android, approve opens the Slack thread, setup cuts the branch and worktree, nine specialists "
                   "consult, the TechLead writes the brief, and the implement session finds dev already fixed it "
                   "(MEMTEMDIS-4611 is in the fixture's dev branch). Review passes the outcome on, and publish reports "
                   "it in Slack, discards the worktree, and opens no pull request. AWS is off with the local fallback "
                   "on, so the session runs on this Mac rather than through session_worker.sh, which the capture "
                   "cannot fake.",
    "source": "jira", "env": {**ENV, "DEVELOPMENT_AWS": "0", "AWS_WORKER_FALLBACK": "1"},
    "files": base_files({"issue": "MEMTEMDIS-4630", "queued_at": 1793180000}, dev_config()),
    "repos": {AND: {"base": "dev", "remote": "fs-eng/android-tree",
                    "files": {**KOTLIN_BASE, "validate.sh": "#!/bin/sh\necho 'BUILD SUCCESSFUL in 41s'\n"},
                    "commits": [{"message": "MEMTEMDIS-4611 Drop a person's cached memories when one is deleted",
                                 "files": AND_FIXED}]}},
    "context": {"job_file": "{{home}}/state/jira-queue/job.json"},
    "fakes": and_fakes,
    "stubs": {"platform": {"platforms": ["android"], "reason": "Android component, and the repro is on Android 8.41.0"},
              "brief": BRIEF_ANDROID}})

# ------------------------------------------------------- platform unclear
vague = jira_issue("MEMTEMDIS-4641", "Memories feels slow for big families",
                   "From the October member feedback digest: several members say memories take a long time to show up "
                   "for people with large families. No device, app version, or screen given.",
                   [], ["member-feedback"])
write(f"{D}/platform-unclear.json", {
    "description": "A vague issue with no component and no platform evidence. PlatformTriage answers no code base, so "
                   "approve posts in Slack that it left the issue for the next poll and declines. Every later stage "
                   "passes the decline on: no worktree, no consults, no session, and publish ends declined.",
    "source": "jira", "env": ENV,
    "files": base_files({"issue": "MEMTEMDIS-4641", "queued_at": 1793180600}, dev_config()),
    "repos": {},
    "context": {"job_file": "{{home}}/state/jira-queue/job.json"},
    "fakes": {"curl": jira_curl(vague)},
    "stubs": {"platform": {"platforms": [],
                           "reason": "no component, screen, or API is named, and the slowness could be in either app or the API"},
              "brief": {"feasible": False, "reason": "setup declined", "anchor": "", "brief": ""}}})

# --------------------------------------------- iOS, review and publish
HEADER = "Apps/FamilySearchTree/Sources/Memories/Albums/AlbumHeaderView.swift"
HEADER_BASE = """import SwiftUI

struct AlbumHeaderView: View {
    let album: MemoryAlbum

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(album.title)
                .font(.headline)
        }
        .padding(.horizontal)
    }
}
"""
HEADER_NEW = """import SwiftUI

struct AlbumHeaderView: View {
    let album: MemoryAlbum

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(album.title)
                .font(.headline)
            Text("^[\\(album.photoCount) photo](inflect: true)")
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal)
    }
}
"""
ALBUM = "Apps/FamilySearchTree/Sources/Memories/Albums/MemoryAlbum.swift"
IOS_FILES = {
    HEADER: HEADER_BASE,
    ALBUM: "import Foundation\n\nstruct MemoryAlbum: Identifiable, Hashable {\n    let id: String\n    let title: String\n    let photoCount: Int\n}\n",
    "validate.sh": "#!/bin/sh\necho '** BUILD SUCCEEDED ** [FamilySearchTree, 198.7 sec]'\necho 'Test Suite All tests passed: 1843 tests, 0 failures'\n",
    "buildScripts/lint/install.sh": "#!/bin/sh\necho 'swiftlint 0.57.0 already installed'\n",
    "buildScripts/lint/fix-changed.sh": "#!/bin/sh\necho 'swiftformat: 0 files formatted'\n",
    "buildScripts/lint/lint-changed.sh": "#!/bin/sh\necho 'Done linting! Found 0 violations, 0 serious in 1 file.'\n",
}
KEY = "MEMTEMDIS-4655"
SESSION_PATCH = "fixture/session-edit.patch"
# The implement session ran on an AWS Mac VM, which the capture cannot fake, and its edit cannot be replayed by a
# fake session either. The worktree setup creates holds it once the review's first lint fix runs: the fixture
# prefixes ios's real lint.fix command with `git apply` of the session's diff. Later rounds skip it harmlessly.
LINT_FIX = (f"/usr/bin/git apply --whitespace=nowarn {{{{home}}}}/{SESSION_PATCH} 2>/dev/null; "
            "buildScripts/lint/install.sh >/dev/null && buildScripts/lint/fix-changed.sh")
ios_issue = jira_issue(KEY, "Show the photo count in the memory album header",
                       "Members can't tell how big an album is until they scroll it. Show the number of photos under "
                       "the album title, pluralized, in secondary text.\n\nDesign: Memories > Album header, Figma frame "
                       "\"Album / Header / Count\".", ["Mobile iOS"], ["memories", "albums"])
WT = "{{home}}/" + IOS + "/.claude/worktrees/" + KEY
PR_BODY = ("## What\n\nShows the album's photo count under its title in the memory album header, pluralized with "
           "automatic grammar agreement (\"1 photo\", \"12 photos\").\n\n## Why\n\nMembers couldn't tell how big an "
           "album was before scrolling it. "
           "[MEMTEMDIS-4655](https://familysearch.atlassian.net/browse/MEMTEMDIS-4655)\n\n## Verification\n\n"
           "- `./run-ios.sh test FamilySearchTree` passed: 1843 tests, 0 failures.\n"
           "- `buildScripts/lint/lint-changed.sh`: 0 violations.\n"
           "- Checked the header with 0, 1, and 214 photos in the iPhone 16 simulator, in English and Spanish.")
IMPLEMENTED = {
    "outcome": "implemented",
    "summary": "The memory album header shows the album's photo count, pluralized, under its title.",
    "verification": "./run-ios.sh test FamilySearchTree: 1843 tests, 0 failures. lint-changed.sh: 0 violations.",
    "pr_title": f"[{KEY}] Show the photo count in the memory album header",
    "pr_body": PR_BODY, "follow_ups": [],
    "placement": {"where": "aws-mac", "session": True, "host": "h-0a1b2c3d4e5f60718"},
    "ran": True, "refined": False, "files": [HEADER], "diff": WT + "/.armature/diff.patch",
    "session_id": "4b0e7f6a-2c1d-4e8f-9a3b-5c6d7e8f9a0b", "model": "us.anthropic.claude-opus-5-5",
    "is_error": False, "denied_tools": [], "cost_usd": 3.87, "duration_ms": 2412000}
PREPARE = {"issue": KEY, "summary": ios_issue["fields"]["summary"], "description": ios_issue["fields"]["description"],
           "type": "Story", "status": "To Do", "components": ["Mobile iOS"], "labels": ["memories", "albums"],
           "component_platforms": ["ios"], "platform": "", "thread_ts": "", "requeued": False,
           "sprint": "MEMTEMDIS Sprint 2026.22", "url": f"https://example.atlassian.net/browse/{KEY}", "ready": True}
APPROVE = {"approved": True, "platform": "ios", "platforms": ["ios"], "siblings": [], "reason": "",
           "thread_ts": SLACK_TS}
BRIEF_IOS = {"feasible": True, "reason": "",
             "anchor": "The memory album header shows the album's photo count, which needs a change in AlbumHeaderView.",
             "brief": "- Reuse: `MemoryAlbum.photoCount` already exists. Use automatic grammar agreement "
                      "(`^[\\(count) photo](inflect: true)`), as PersonMemoriesHeader does, instead of a new plural rule.\n"
                      "- Where: `AlbumHeaderView.body` in Apps/FamilySearchTree/Sources/Memories/Albums/AlbumHeaderView.swift.\n"
                      "- Constraints: `.subheadline` and `.foregroundStyle(.secondary)` to match the design (beauty); "
                      "no new model field (simplicity).\n"
                      "- Decided: no accessibility label override; the inflected text reads correctly in VoiceOver (beauty over conventions).\n"
                      "- Out of scope: the album grid's own loading spinner."}
LEAD_APPROVE = {"verdict": "approve",
                "summary": "Small, correct change: the header reads photoCount and pluralizes with inflection.",
                "decisions": [], "comments": []}
ios_fakes = {
    "git": GIT_NEW_BRANCH,
    "gh": [{"match": "^api user --jq .login", "stdout": "stephanheilner\n"},
           {"match": f"^pr create --repo fs-eng/mobile-ios --base master --head {KEY.lower()}",
            "stdout": "https://github.com/fs-eng/mobile-ios/pull/9231\n"},
           {"match": "^api repos/fs-eng/mobile-ios/pulls/9231 --jq .merged", "stdout": "false\n"},
           {"match": "^api repos/fs-eng/mobile-ios/pulls/9231 --jq .state", "stdout": "open\n"},
           {"match": "^api repos/fs-eng/mobile-ios/pulls/9231/files --paginate --slurp", "stdout": [[
               {"filename": HEADER, "status": "modified", "additions": 3, "deletions": 0,
                "patch": patch(HEADER, HEADER_BASE, HEADER_NEW).split("\n", 2)[2]}]]},
           {"match": "^api repos/fs-eng/mobile-ios/pulls/9231/reviews --method POST --input -",
            "stdout": {"id": 2163310, "state": "COMMENTED",
                       "html_url": "https://github.com/fs-eng/mobile-ios/pull/9231#pullrequestreview-2163310"}},
           {"match": "^api repos/fs-eng/mobile-ios/issues/9231/comments -f body=",
            "stdout": {"id": 3270402, "html_url": "https://github.com/fs-eng/mobile-ios/pull/9231#issuecomment-3270402"}},
           {"match": "--paginate --slurp", "stdout": [[]]}],
    "claude": [{"match": "--json-schema", "result": {"result": "", "structured_output": LEAD_APPROVE}},
               {"match": "", "result": "No findings."}],
}
write(f"{D}/ios-review-publish.json", {
    "description": "An iOS story after its implement session, from recorded prepare, approve, brief, and implement "
                   "outputs. Setup cuts the branch and worktree, the local review (nine roles, the build, the team lead) "
                   "approves in one round, and publish pushes the reviewed commit, opens the pull request, posts the "
                   "review to it, and reports in the issue's Slack thread. The implement session ran in an AWS Mac VM "
                   "that the capture cannot fake, so its stage is recorded, and its edit reaches the worktree through "
                   "the fixture's ios lint.fix command, which applies fixture/session-edit.patch before the real fixer "
                   "in the review's first round.",
    "source": "jira", "env": ENV,
    "files": {**base_files({"issue": KEY, "queued_at": 1793181200}, dev_config(LINT_FIX)),
              SESSION_PATCH: patch(HEADER, HEADER_BASE, HEADER_NEW),
              f"state/jira-work/{KEY}/prs.json": {"platforms": ["ios"], "prs": {}, "thread_ts": SLACK_TS}},
    "jira": {"issue": {"key": KEY, "fields": {"summary": ios_issue["fields"]["summary"],
                                              "description": ios_issue["fields"]["description"],
                                              "status": {"name": "In Progress"}, "issuetype": {"name": "Story"},
                                              "comment": {"comments": []}}},
             "transitions": [{"name": "In Review"}, {"name": "Done"}]},
    "repos": {IOS: {"base": "master", "remote": "fs-eng/mobile-ios", "files": IOS_FILES}},
    "stages": ["setup", "review", "publish"],
    "context": {"job_file": "{{home}}/state/jira-queue/job.json", "prepare": PREPARE,
                "platform": {"platforms": ["ios"], "reason": "Mobile iOS component; the album header is a SwiftUI view."},
                "approve": APPROVE, "brief": BRIEF_IOS, "implement": IMPLEMENTED},
    "fakes": ios_fakes, "stubs": {}})
