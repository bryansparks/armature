import difflib
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from fx_common import *

D = __file__.rsplit("/", 2)[0] + "/pr-fix"

# ---------------------------------------------------------------- iOS repo
IOS = "state/repos/fs-eng_mobile-ios"
IOS_BRANCH = "MEMTEMDIS-4588-pause-memory-audio"
IOS_HEAD = f"{{{{rev:{IOS}:{IOS_BRANCH}}}}}"
PLAYER = "Apps/FamilySearchTree/Sources/Memories/Audio/MemoryAudioPlayer.swift"
PLAYER_BASE = """import AVFoundation

final class MemoryAudioPlayer {
    private let player: AVPlayer

    init(url: URL) {
        player = AVPlayer(url: url)
    }

    func play() {
        player.play()
    }

    func pause() {
        player.pause()
    }
}
"""
PLAYER_PR = """import AVFoundation

final class MemoryAudioPlayer {
    private let player: AVPlayer
    private var interruptionObserver: NSObjectProtocol?

    init(url: URL) {
        player = AVPlayer(url: url)
        interruptionObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: nil,
            queue: .main
        ) { [unowned self] notification in
            let type = AVAudioSession.InterruptionType(rawValue: notification.userInfo![AVAudioSessionInterruptionTypeKey] as! UInt)!
            if type == .began {
                self.pause()
            }
        }
    }

    func play() {
        player.play()
    }

    func pause() {
        player.pause()
    }
}
"""
PLAYER_FIXED = """import AVFoundation

final class MemoryAudioPlayer {
    private let player: AVPlayer
    private var interruptionObserver: NSObjectProtocol?

    init(url: URL) {
        player = AVPlayer(url: url)
        interruptionObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: nil,
            queue: .main
        ) { [weak self] notification in
            guard let raw = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                  AVAudioSession.InterruptionType(rawValue: raw) == .began else { return }
            self?.pause()
        }
    }

    deinit {
        if let interruptionObserver {
            NotificationCenter.default.removeObserver(interruptionObserver)
        }
    }

    func play() {
        player.play()
    }

    func pause() {
        player.pause()
    }
}
"""
# Only the force-unwrap finding applied; the observer finding skipped.
PLAYER_PARTIAL = PLAYER_PR.replace(
    """        ) { [unowned self] notification in
            let type = AVAudioSession.InterruptionType(rawValue: notification.userInfo![AVAudioSessionInterruptionTypeKey] as! UInt)!
            if type == .began {
                self.pause()
            }
        }""",
    """        ) { [unowned self] notification in
            guard let raw = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                  AVAudioSession.InterruptionType(rawValue: raw) == .began else { return }
            self.pause()
        }""")


def patch(path, before, after):
    lines = difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{path}", f"b/{path}")
    return "".join(lines)


IOS_FILES = {
    PLAYER: PLAYER_BASE,
    "validate.sh": "#!/bin/sh\necho '** BUILD SUCCEEDED ** [FamilySearchTree, 212.4 sec]'\necho 'Test Suite All tests passed: 1843 tests, 0 failures'\n",
    "buildScripts/lint/install.sh": "#!/bin/sh\necho 'swiftlint 0.57.0 already installed'\n",
    "buildScripts/lint/fix-changed.sh": "#!/bin/sh\necho 'swiftformat: 0 files formatted'\n",
    "buildScripts/lint/lint-changed.sh": "#!/bin/sh\necho 'Done linting! Found 0 violations, 0 serious in 1 file.'\n",
}
IOS_REPO = {"base": "master", "branch": IOS_BRANCH, "remote": "fs-eng/mobile-ios", "files": IOS_FILES,
            "commits": [{"message": "MEMTEMDIS-4588 Pause memory audio when the session is interrupted",
                         "files": {PLAYER: PLAYER_PR}}]}

# The fake Claude session cannot edit files. The repository's real fix_commands entry runs in the
# worktree right after the session, so the fixture prefixes it with `git apply` of the canned edit the
# session reports. That is the only way the session's change reaches the worktree under capture.
SESSION_PATCH = "fixture/session-edit.patch"
FIX_CMD = (f"/usr/bin/git apply --whitespace=nowarn {{{{home}}}}/{SESSION_PATCH} && buildScripts/lint/install.sh >/dev/null "
           "&& buildScripts/lint/fix-changed.sh")

F1, F2 = "1a7c3e9d0b42", "5e0d8f61c2a7"
C1, C2 = 1850001, 1850002
REVIEW_ID = 2160001
LINE_FORCE, LINE_OBS = 14, 13
F1_BODY = ("These force unwraps crash the app when an interruption arrives without "
           "`AVAudioSessionInterruptionTypeKey`, which happens on some iOS 17 route changes. Read the key "
           "with `as? UInt` and bail out instead.\n\n```suggestion\n"
           "            guard let raw = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,\n"
           "                  AVAudioSession.InterruptionType(rawValue: raw) == .began else { return }\n```\n\n"
           f"<!-- armature-finding:{F1} -->")
F2_BODY = ("The observer is never removed, and `[unowned self]` traps if an interruption fires after the "
           "player is released, such as when the memory detail screen closes mid-playback. Capture `self` "
           "weakly and remove the observer in `deinit`.\n\n"
           f"<!-- armature-finding:{F2} -->")
REVIEW_BODY = ("## :construction: Changes Recommended\n\n"
               "Two blocking issues in the interruption handler: it can crash on a missing key, and it "
               "outlives the player.\n\n"
               "Comment `/armature fix` to have Armature apply these changes.")


def ios_comment(cid, line, body, head):
    return {"id": cid, "pull_request_review_id": REVIEW_ID, "path": PLAYER, "line": line,
            "original_line": line, "in_reply_to_id": None, "commit_id": head, "original_commit_id": head,
            "user": {"login": "fs-armature", "type": "User"}, "body": body,
            "html_url": f"https://github.com/fs-eng/mobile-ios/pull/9214#discussion_r{cid}",
            "created_at": "2026-10-27T16:42:10Z", "updated_at": "2026-10-27T16:42:10Z",
            "author_association": "MEMBER"}


def ios_review(head):
    return {"id": REVIEW_ID, "node_id": "PRR_kwDOAbc1234M4ugAB", "user": {"login": "fs-armature", "type": "User"},
            "body": REVIEW_BODY, "state": "COMMENTED",
            "html_url": f"https://github.com/fs-eng/mobile-ios/pull/9214#pullrequestreview-{REVIEW_ID}",
            "commit_id": head, "submitted_at": "2026-10-27T16:42:11Z", "author_association": "MEMBER"}


def findings_state():
    def rec(fid, line, claim, body, cid, raised):
        return {"id": fid, "path": PLAYER, "line": line, "start_line": None, "claim": claim,
                "raised_by": raised, "blocking": True, "severity": "high", "body": body,
                "code_hash": "", "disposition": "keep", "head": IOS_HEAD, "comment_id": cid}
    return {"repo": "fs-eng/mobile-ios", "number": 9214, "head": IOS_HEAD, "run_id": "run-7f3a9c",
            "updated_at": 1793119331.0, "not_approved": ["2 blocking findings"], "gates": {},
            "approved_head": "",
            "review_url": f"https://github.com/fs-eng/mobile-ios/pull/9214#pullrequestreview-{REVIEW_ID}",
            "findings": [rec(F1, LINE_FORCE, "Force unwraps crash on a missing interruption type", F1_BODY, C1,
                             ["correctness"]),
                         rec(F2, LINE_OBS, "Observer outlives the player", F2_BODY, C2,
                             ["concurrency", "correctness"])],
            "decisions": [], "comments": [], "lead_verdict": "comment",
            "lead_summary": "Two blocking issues in the interruption handler."}


def pr_view_ios():
    return {"state": "OPEN", "isDraft": False, "isCrossRepository": False, "headRefOid": IOS_HEAD,
            "baseRefName": "master", "headRefName": IOS_BRANCH, "author": {"login": "bkimball-fs"},
            "title": "MEMTEMDIS-4588 Pause memory audio on interruption",
            "body": "Pauses a memory's audio when a call or Siri interrupts the audio session.\n\n"
                    "Fixes [MEMTEMDIS-4588](https://familysearch.atlassian.net/browse/MEMTEMDIS-4588).",
            "mergeCommit": None}


LEAD_APPROVE = {"verdict": "approve",
                "summary": "Both findings are fixed: the handler reads the key safely and the observer is removed in deinit.",
                "decisions": [{"claim": "Force unwraps crash on a missing interruption type", "raised_by": ["correctness"],
                               "decision": "drop", "reason": "Fixed: the key is read with as? UInt.",
                               "disposition": "resolved", "finding_id": F1},
                              {"claim": "Observer outlives the player", "raised_by": ["concurrency", "correctness"],
                               "decision": "drop", "reason": "Fixed: weak capture and removal in deinit.",
                               "disposition": "resolved", "finding_id": F2}],
                "comments": []}
LEAD_BLOCK = {"verdict": "comment",
              "summary": "The force unwraps are gone, but the observer still outlives the player.",
              "decisions": [{"claim": "Force unwraps crash on a missing interruption type", "raised_by": ["correctness"],
                             "decision": "drop", "reason": "Fixed: the key is read with as? UInt.",
                             "disposition": "resolved", "finding_id": F1},
                            {"claim": "Observer outlives the player", "raised_by": ["concurrency", "correctness"],
                             "decision": "block", "reason": "Still [unowned self] and no deinit removes the observer.",
                             "disposition": "keep", "finding_id": F2}],
              "comments": [{"path": PLAYER, "line": 13, "severity": "high", "blocking": True,
                            "claim": "Observer outlives the player", "raised_by": ["concurrency", "correctness"],
                            "finding_id": F2,
                            "body": "The observer is still never removed, and `[unowned self]` traps if an interruption arrives after the player is released. Capture `self` weakly and remove the observer in `deinit`.",
                            "verified": "No deinit in MemoryAudioPlayer; the closure still captures unowned self."}]}

FIX_APPLIED = {"applied": [{"id": f"thread-{C1}", "path": PLAYER, "line": LINE_FORCE,
                            "change": "Read the interruption type with as? UInt and return early instead of force unwrapping."},
                           {"id": f"thread-{C2}", "path": PLAYER, "line": LINE_OBS,
                            "change": "Captured self weakly and removed the observer in a new deinit."}],
               "skipped": []}
FIX_PARTIAL = {"applied": [{"id": f"thread-{C1}", "path": PLAYER, "line": LINE_FORCE,
                            "change": "Read the interruption type with as? UInt and return early instead of force unwrapping."}],
               "skipped": [{"id": f"thread-{C2}", "path": PLAYER, "line": LINE_OBS,
                            "reason": "Removing the observer in deinit needs the player's owner to stop retaining it first, and that code is outside this pull request."}]}
REFINE_SKIP = {"applied": [],
               "skipped": [{"path": PLAYER, "line": 13,
                            "reason": "Same as before: the player's owner retains it past the screen, so a deinit would never run. That needs a change outside this pull request."}]}


BLOCK_REVIEWS = {
    "correctness": "1. MemoryAudioPlayer.swift:13 (high): the interruption observer is never removed. `[unowned self]` "
                   "traps if an interruption is posted after the player is released, which happens when the memory "
                   "detail screen closes mid-playback. Confidence: high, nothing calls removeObserver.",
    "concurrency": "1. MemoryAudioPlayer.swift:13 (high): the block observer outlives MemoryAudioPlayer. It runs on "
                   "the main queue after the screen that owns the player is gone, and the unowned capture crashes "
                   "there. Use [weak self] and remove the observer in deinit.",
}


def ios_fakes(lead, fix_report, refine=None, reviews=None):
    gh = [
        {"match": "^api repos/fs-eng/mobile-ios/collaborators/bkimball-fs/permission --jq .permission", "stdout": "write\n"},
        {"match": "^api repos/fs-eng/mobile-ios/issues/comments/3260001/reactions -f content=eyes",
         "stdout": {"id": 412000771, "content": "eyes"}},
        {"match": "^pr view 9214 --repo fs-eng/mobile-ios --json", "stdout": pr_view_ios()},
        {"match": "^api user --jq .login", "stdout": "fs-armature\n"},
        {"match": f"^api repos/fs-eng/mobile-ios/pulls/9214/reviews/{REVIEW_ID}/comments --paginate --slurp",
         "stdout": [[ios_comment(C1, LINE_FORCE, F1_BODY, IOS_HEAD), ios_comment(C2, LINE_OBS, F2_BODY, IOS_HEAD)]]},
        {"match": "^api repos/fs-eng/mobile-ios/pulls/9214/reviews --paginate --slurp", "stdout": [[ios_review(IOS_HEAD)]]},
        {"match": "^api repos/fs-eng/mobile-ios/pulls/9214/comments --paginate --slurp",
         "stdout": [[ios_comment(C1, LINE_FORCE, F1_BODY, IOS_HEAD), ios_comment(C2, LINE_OBS, F2_BODY, IOS_HEAD)]]},
        {"match": "^api repos/fs-eng/mobile-ios/issues/9214/comments --paginate --slurp", "stdout": [[
            {"id": 3260001, "user": {"login": "bkimball-fs", "type": "User"}, "body": "/armature fix",
             "created_at": "2026-10-28T15:02:44Z", "author_association": "MEMBER",
             "html_url": "https://github.com/fs-eng/mobile-ios/pull/9214#issuecomment-3260001"}]]},
        {"match": "^api repos/fs-eng/mobile-ios/pulls/9214/files --paginate --slurp", "stdout": [[
            {"filename": PLAYER, "status": "modified", "additions": 20, "deletions": 0,
             "patch": patch(PLAYER, PLAYER_BASE, PLAYER_FIXED).split("\n", 2)[2]}]]},
        {"match": "^api repos/fs-eng/mobile-ios/pulls/9214 --jq .merged", "stdout": "false\n"},
        {"match": "^api repos/fs-eng/mobile-ios/pulls/9214 --jq .state", "stdout": "open\n"},
        {"match": "^api repos/fs-eng/mobile-ios/pulls/9214/reviews --method POST --input -",
         "stdout": {"id": 2160002, "state": "APPROVED" if lead["verdict"] == "approve" else "COMMENTED",
                    "html_url": "https://github.com/fs-eng/mobile-ios/pull/9214#pullrequestreview-2160002"}},
        {"match": "^api graphql", "stdout": {"data": {"repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"id": "PRRT_kwDOAbc1234M5Aa01", "isResolved": False, "comments": {"nodes": [{"databaseId": C1}]}},
                      {"id": "PRRT_kwDOAbc1234M5Aa02", "isResolved": False, "comments": {"nodes": [{"databaseId": C2}]}}]}}}}}},
        {"match": "^api repos/fs-eng/mobile-ios/pulls/9214/comments/\\d+/replies", "stdout": {"id": 1850101}},
        {"match": "^api repos/fs-eng/mobile-ios/issues/9214/comments -f body=",
         "stdout": {"id": 3260002, "html_url": "https://github.com/fs-eng/mobile-ios/pull/9214#issuecomment-3260002"}},
        {"match": "--paginate --slurp", "stdout": [[]]},
    ]
    claude = [{"match": "--json-schema", "result": {"result": "", "structured_output": lead}}]
    if refine is not None:
        claude.append({"match": "These findings come from Armature's review of the edits you already made",
                       "result": json.dumps(refine)})
    claude.append({"match": "--restricted", "result": json.dumps(fix_report)})
    claude += [{"match": f"Your role is {role}", "result": text} for role, text in (reviews or {}).items()]
    claude.append({"match": "", "result": "No findings."})
    return {"gh": gh, "claude": claude}


# One role session at a time: the capture fakes share a call counter file that parallel sessions can
# corrupt, and a serial loop also keeps the recorded session order stable. Fixed git dates keep the
# locally committed fix head, and so every recorded sha, the same from one capture to the next.
SERIAL = {"PR_FIX_REVIEW_FAN_OUT": "1",
          "GIT_AUTHOR_DATE": "2026-10-28T15:10:00", "GIT_COMMITTER_DATE": "2026-10-28T15:10:00"}


def ios_fixture(desc, fakes, after, env=None):
    job = {"repo": "fs-eng/mobile-ios", "number": 9214, "platform": "ios", "requester": "bkimball-fs",
           "comment_id": 3260001, "scope": "pr", "mode": "request"}
    files = {"config/teams.json": teams(), "config/roles.json": roles(),
             "state/pr-fix-queue/job.json": job,
             "state/pr-findings/fs-eng_mobile-ios-9214.json": findings_state(),
             "config/pr-authors.json": {"authors": {"fs-eng/mobile-ios": ["bkimball-fs"]}},
             "config/validate.json": {"commands": {"fs-eng/mobile-ios": "./validate.sh --quick --base {base}"},
                                      "fix_commands": {"fs-eng/mobile-ios": FIX_CMD}},
             SESSION_PATCH: patch(PLAYER, PLAYER_PR, after)}
    return {"description": desc, "source": "github", "env": {**SERIAL, **(env or {})}, "files": files,
            "repos": {IOS: IOS_REPO}, "context": {"job_file": "{{home}}/state/pr-fix-queue/job.json"},
            "fakes": fakes, "stubs": {}}


write(f"{D}/pushed-after-review.json", ios_fixture(
    "A write collaborator comments /armature fix on an iOS pull request with Armature's two blocking findings. "
    "The fix session applies both, the local review approves in one round, and the fix is pushed with that review "
    "posted and both threads resolved. The fake session cannot edit, so the fixture's fix_commands entry applies "
    "the session's canned edit (fixture/session-edit.patch) right after it, in the lint-fix step.",
    ios_fakes(LEAD_APPROVE, FIX_APPLIED), PLAYER_FIXED))

write(f"{D}/review-not-approved.json", ios_fixture(
    "The fix session applies one finding and skips the other. The local review still blocks on the skipped "
    "finding, the refine session skips it again and changes nothing, so the loop stops and nothing is pushed: "
    "the requester gets a reply with what is left. The session's canned edit reaches the worktree through the "
    "fixture's fix_commands entry, which fails harmlessly on the refine because the edit is already applied.",
    ios_fakes(LEAD_BLOCK, FIX_PARTIAL, refine=REFINE_SKIP, reviews=BLOCK_REVIEWS), PLAYER_PARTIAL))

# ------------------------------------------------------------- android, refused
AND = "state/repos/fs-eng_android-tree"
denied = {"gh": [{"match": "^api repos/fs-eng/android-tree/collaborators/mrasmussen-contractor/permission --jq .permission",
                  "stdout": "read\n"}]}
job = {"repo": "fs-eng/android-tree", "number": 4907, "platform": "android", "requester": "mrasmussen-contractor",
       "comment_id": 3271145, "scope": "pr", "mode": "request"}
write(f"{D}/no-write-access.json", {
    "description": "Someone with only read access comments /armature fix on an Android pull request. Prepare declines "
                   "on the permission check, every later stage passes the decline on, and nothing is posted, so the "
                   "bot cannot be made to speak.",
    "source": "github", "env": {},
    "files": {"config/teams.json": teams(), "config/roles.json": roles(), "state/pr-fix-queue/job.json": job,
              "config/validate.json": {"commands": {"fs-eng/android-tree": "./validate.sh --quick --base {base}"}}},
    "repos": {AND: {"base": "dev", "branch": "MEMTEMDIS-4602-memories-empty-state", "remote": "fs-eng/android-tree",
                    "files": KOTLIN_BASE,
                    "commits": [{"message": "MEMTEMDIS-4602 Show an empty state when a person has no memories",
                                 "files": KOTLIN_CHANGE}]}},
    "context": {"job_file": "{{home}}/state/pr-fix-queue/job.json"},
    "fakes": denied, "stubs": {}})
