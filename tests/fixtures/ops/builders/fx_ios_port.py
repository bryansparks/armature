import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from fx_common import OPS, neutral, roles, write

D = __file__.rsplit("/", 2)[0] + "/ios-port"
os.makedirs(D, exist_ok=True)

GITCONFIG = "[user]\n\tname = Stephan Heilner\n\temail = armature@armature-ops.invalid\n"
ENV = {"IOS_PORT_AWS": "0", "AWS_WORKER_FALLBACK": "1", "SLACK_OWNER_USER": "U04F2KX9B7Q",
       "GIT_AUTHOR_DATE": "2026-10-28 09:30:00 -0600", "GIT_COMMITTER_DATE": "2026-10-28 09:30:00 -0600"}
SLACK_TS = "1700000000.000100"

def development():
    data = neutral(json.loads(Path(f"{OPS}/config/development.json").read_text()))
    android = data["platforms"]["android"]
    android["checkout"] = "{{home}}/android-tree"
    data["platforms"] = {"android": android}
    return data

def ios_port_cfg():
    return json.loads(Path(f"{OPS}/config/ios-port.json").read_text())

CLAUDE_JSON = {"mcpServers": {"jira": {"command": "uvx", "args": ["mcp-atlassian"],
                                       "env": {"JIRA_URL": "https://example.atlassian.net"}}}}

# ---------------------------------------------------------------- the iOS source, fs-eng/mobile-ios
IOS = "state/repos/fs-eng_mobile-ios"
CARD = "shared/TempleUI/Sources/TempleUI/Reservations/ReservationCardView.swift"
PILL = "shared/TempleUI/Sources/TempleUI/Components/StatusPill.swift"
STRINGS = "shared/TempleUI/Sources/TempleUI/Resources/Base.lproj/Localizable.strings"
CARD_BASE = """import SwiftUI

struct ReservationCardView: View {
    let reservation: Reservation
    let onCancel: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(reservation.ordinanceName)
                .font(.headline)
                .foregroundStyle(Color.templeTextPrimary)
            Text(reservation.templeName)
                .font(.subheadline)
                .foregroundStyle(Color.templeTextSecondary)
            Text(reservation.startsAt, format: .dateTime.weekday(.wide).month().day().hour().minute())
                .font(.subheadline)
                .foregroundStyle(Color.templeTextSecondary)
            StatusPill(status: reservation.status)
            Button(String(localized: "temple.reservation.cancel", bundle: .module), role: .destructive, action: onCancel)
                .buttonStyle(.borderless)
        }
        .padding(16)
        .background(Color.templeSurface, in: RoundedRectangle(cornerRadius: 12))
    }
}
"""
CARD_HEAD = (CARD_BASE.replace("spacing: 8", "spacing: 4")
             .replace("                .font(.headline)\n", "                .font(.subheadline.weight(.semibold))\n")
             .replace(".padding(16)", ".padding(12)"))
PILL_BASE = """import SwiftUI

struct StatusPill: View {
    let status: ReservationStatus

    var body: some View {
        Text(status.title)
            .font(.caption.weight(.medium))
            .padding(.horizontal, 8)
            .padding(.vertical, 2)
            .foregroundStyle(.white)
            .background(Color.blue, in: Capsule())
    }
}
"""
PILL_HEAD = PILL_BASE.replace("Color.blue", "Color.templeAccent")
STRINGS_BASE = ('/* Title of the reservation details screen. */\n"temple.reservation.title" = "Reservation";\n\n'
                '/* Destructive button on a reservation card. */\n"temple.reservation.cancel" = "Cancel Reservation";\n')
THEME = """import SwiftUI

public extension Color {
    static let templeTextPrimary = Color("TextPrimary", bundle: .module)
    static let templeTextSecondary = Color("TextSecondary", bundle: .module)
    static let templeSurface = Color("Surface", bundle: .module)
    static let templeAccent = Color("Accent", bundle: .module)
}
"""
IOS_FILES = {CARD: CARD_BASE, PILL: PILL_BASE, STRINGS: STRINGS_BASE,
             "shared/TempleUI/Sources/TempleUI/Theme/Color+Temple.swift": THEME,
             "CLAUDE.md": "# mobile-ios\n\nAgent notes for the iOS app.\n"}
CARD_BRANCH = "TEMPLE-2210-reservation-card-spacing"
PILL_BRANCH = "ryan/ordinance-pill-accent"

def ios_repo(branch, message, files):
    return {"base": "develop", "branch": branch, "remote": "fs-eng/mobile-ios", "files": IOS_FILES,
            "commits": [{"message": message, "files": files}]}

def unified(changes):
    """The diff GitHub's compare API returns for these changes, made by git from the same contents."""
    with tempfile.TemporaryDirectory() as tmp:
        g = ["git", "-C", tmp, "-c", "user.name=x", "-c", "user.email=x@x", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q", tmp], check=True)
        for files in (IOS_FILES, changes):
            for name, text in files.items():
                os.makedirs(os.path.dirname(os.path.join(tmp, name)) or tmp, exist_ok=True)
                Path(tmp, name).write_text(text)
            subprocess.run([*g, "add", "-A"], check=True)
            subprocess.run([*g, "commit", "-q", "-m", "x"], check=True)
        return subprocess.run([*g, "diff", "HEAD~1", "HEAD"], capture_output=True, text=True, check=True).stdout

def ios_pr(number, title, branch, author="RyanPlumb", state="OPEN", draft=False):
    return {"number": number, "title": title, "url": f"https://github.com/fs-eng/mobile-ios/pull/{number}",
            "state": state, "isDraft": draft, "isCrossRepository": False,
            "author": {"id": "MDQ6VXNlcjU4MzIxOTA3", "is_bot": False, "login": author, "name": "Ryan Plumb" if author == "RyanPlumb" else "Kara Jensen"},
            "headRefName": branch, "headRefOid": f"{{{{rev:{IOS}:{branch}}}}}",
            "baseRefOid": f"{{{{rev:{IOS}:develop}}}}"}

def source_fakes(number, branch, changes, body):
    head, base = f"{{{{rev:{IOS}:{branch}}}}}", f"{{{{rev:{IOS}:develop}}}}"
    files = [{"status": "modified", "filename": name} for name in sorted(changes)]
    out = [{"match": f"^api -H Accept: application/vnd.github.diff repos/fs-eng/mobile-ios/compare/{base}\\.\\.\\.{head}$",
            "stdout": unified(changes)},
           {"match": f"^pr view {number} --repo fs-eng/mobile-ios --json body --jq .body$", "stdout": body + "\n"},
           {"match": f"^api repos/fs-eng/mobile-ios/compare/{base}\\.\\.\\.{head} --jq ", "stdout": json.dumps(files) + "\n"}]
    for name in sorted(changes):
        out.append({"match": f"^api -H Accept: application/vnd.github.raw repos/fs-eng/mobile-ios/contents/{name}\\?ref={head}$",
                    "stdout": changes[name]})
    return out

# ---------------------------------------------------------------- the Android target, fs-eng/android-tree
TPL = "ui-templex/src/main/java/org/familysearch/mobile/tree/ui/templex"
A_CARD = f"{TPL}/reservations/ReservationCard.kt"
A_PILL = f"{TPL}/components/StatusPill.kt"
A_SPACING = f"{TPL}/theme/TempleSpacing.kt"
A_STRINGS = "ui-templex/src/main/res/values/strings.xml"
A_CARD_BASE = """package org.familysearch.mobile.tree.ui.templex.reservations

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import org.familysearch.mobile.tree.ui.templex.R
import org.familysearch.mobile.tree.ui.templex.components.StatusPill
import org.familysearch.mobile.tree.ui.templex.theme.TempleSpacing

@Composable
fun ReservationCard(reservation: ReservationUi, onCancel: () -> Unit, modifier: Modifier = Modifier) {
    Card(modifier = modifier) {
        Column(
            modifier = Modifier.padding(TempleSpacing.large),
            verticalArrangement = Arrangement.spacedBy(TempleSpacing.small),
        ) {
            Text(reservation.ordinanceName, style = MaterialTheme.typography.titleMedium)
            Text(
                reservation.templeName,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Text(
                reservation.startsAtLabel,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            StatusPill(reservation.status)
            TextButton(onClick = onCancel) {
                Text(stringResource(R.string.templex_reservation_cancel), color = MaterialTheme.colorScheme.error)
            }
        }
    }
}
"""
# The port as the session leaves it, with the card's padding as a literal the reviewers catch.
A_CARD_PORT = (A_CARD_BASE.replace("import androidx.compose.ui.res.stringResource\n",
                                   "import androidx.compose.ui.res.stringResource\nimport androidx.compose.ui.unit.dp\n")
               .replace("Modifier.padding(TempleSpacing.large)", "Modifier.padding(12.dp)")
               .replace("Arrangement.spacedBy(TempleSpacing.small)", "Arrangement.spacedBy(TempleSpacing.xsmall)")
               .replace("style = MaterialTheme.typography.titleMedium)", "style = MaterialTheme.typography.titleSmall)"))
A_PILL_SRC = """package org.familysearch.mobile.tree.ui.templex.components

import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import org.familysearch.mobile.tree.ui.templex.reservations.ReservationStatus
import org.familysearch.mobile.tree.ui.templex.theme.TempleSpacing

@Composable
fun StatusPill(status: ReservationStatus, modifier: Modifier = Modifier) {
    Surface(modifier = modifier, shape = MaterialTheme.shapes.extraLarge, color = MaterialTheme.colorScheme.primary) {
        Text(
            stringResource(status.title),
            modifier = Modifier.padding(horizontal = TempleSpacing.small, vertical = TempleSpacing.xxsmall),
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onPrimary,
        )
    }
}
"""
A_SPACING_SRC = """package org.familysearch.mobile.tree.ui.templex.theme

import androidx.compose.ui.unit.dp

object TempleSpacing {
    val xxsmall = 2.dp
    val xsmall = 4.dp
    val small = 8.dp
    val medium = 12.dp
    val large = 16.dp
}
"""
A_STRINGS_BASE = ('<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
                  '    <string name="templex_reservation_title">Reservation</string>\n'
                  '    <string name="templex_reservation_cancel">Cancel Reservation</string>\n</resources>\n')
ANDROID_FILES = {".gitignore": "/.claude/worktrees/\n/local.properties\nbuild/\n.gradle/\n",
                 A_CARD: A_CARD_BASE, A_PILL: A_PILL_SRC, A_SPACING: A_SPACING_SRC, A_STRINGS: A_STRINGS_BASE,
                 ".claude/skills/nephi-bot/SKILL.md": "---\nname: nephi-bot\ndescription: Per-issue agent for android-tree.\n---\n\nWork one issue end to end in its worktree.\n"}
ANDROID_REPO = {"base": "dev", "remote": "fs-eng/android-tree", "files": ANDROID_FILES}

def common_files(job_number):
    return {".gitconfig": GITCONFIG, ".claude.json": CLAUDE_JSON, "config/ios-port.json": ios_port_cfg(),
            "config/development.json": development(), "config/roles.json": roles(),
            f"state/port-queue/fs-eng_mobile-ios-{job_number}.json":
                {"repo": "fs-eng/mobile-ios", "number": job_number, "queued_at": 1761665400.0}}

def job_context(number):
    return {"job_file": "{{home}}/state/port-queue/fs-eng_mobile-ios-" + str(number) + ".json"}

# ======================================================================== ported-draft-pr
N1 = 8841
ID1 = f"ios-{N1}"
WT1 = f"android-tree/.claude/worktrees/{ID1}"
# One iOS file: the fake gh reads stdin, so in ios_port_source.sh's per-file loop it would swallow the
# rest of the list, and a second file's fetch would be missing from the capture.
CARD_CHANGES = {CARD: CARD_HEAD}
BODY1 = ("Tightens the reservation card to match the new Temple spec in Figma.\n\n"
         "- Card padding 16 → 12, row spacing 8 → 4\n- Ordinance name is now subheadline semibold\n\nTEMPLE-2210")
files1 = common_files(N1)
# The worktree setup made, holding the session's change, because a canned session cannot edit files.
# Only the index is missing, and implement's git add builds it from these files.
files1.update({
    f"android-tree/.git/worktrees/{ID1}/HEAD": f"ref: refs/heads/{CARD_BRANCH}\n",
    f"android-tree/.git/worktrees/{ID1}/commondir": "../..\n",
    f"android-tree/.git/worktrees/{ID1}/gitdir": f"{{{{home}}}}/{WT1}/.git\n",
    f"android-tree/.git/refs/heads/{CARD_BRANCH}": "{{rev:android-tree:dev}}\n",
    "android-tree/.git/info/exclude": "/.armature/\n",
    f"{WT1}/.git": f"gitdir: {{{{home}}}}/android-tree/.git/worktrees/{ID1}\n",
    **{f"{WT1}/{name}": text for name, text in ANDROID_FILES.items()},
    f"{WT1}/{A_CARD}": A_CARD_PORT,
    f"state/jira-work/{ID1}/.keep": "",
})
SETUP1 = {"ready": True, "issue": "TEMPLE-2210", "id": ID1, "kind": "ios-port", "platform": "android",
          "repo": "fs-eng/android-tree", "checkout": "{{home}}/android-tree", "base": "dev", "branch": CARD_BRANCH,
          "worktree": "{{home}}/" + WT1, "head": "{{rev:android-tree:dev}}", "thread_ts": SLACK_TS,
          "scratch": "{{home}}/state/jira-work/" + ID1, "tracker": "jira", "url": ""}
SCREENS = f"{{{{home}}}}/{WT1}/.armature/screens"
PR_BODY1 = ("Ports [fs-eng/mobile-ios#8841](https://github.com/fs-eng/mobile-ios/pull/8841) to Android for "
            "[TEMPLE-2210](https://familysearch.atlassian.net/browse/TEMPLE-2210).\n\n"
            "## What changed\n\n- `ReservationCard`: padding 16dp → 12dp and spacing 8dp → 4dp, matching iOS points.\n"
            "- The ordinance name uses `titleSmall`, Material's counterpart of iOS subheadline semibold.\n\n"
            "## Material over iOS\n\nThe cancel action stays a `TextButton` in the error color. iOS uses a borderless "
            "destructive button, which Material expresses this way.\n\n## Verification\n\n"
            "`./gradlew :ui-templex:testDebugUnitTest` and `./gradlew :app:assembleDebug` pass. Checked the "
            "reservation card on the emulator in light and dark.")
IMPLEMENT1 = ("I read the iOS diff, the changed files under after/, and StatusPill and Color+Temple in source/. "
              "The card is ReservationCard in ui-templex, and TempleSpacing has the 4dp step.\n\n"
              + json.dumps({"outcome": "implemented",
                            "summary": "The Temple reservation card on Android now uses the tighter padding, spacing, and title style from iOS.",
                            "verification": "./gradlew :ui-templex:testDebugUnitTest (BUILD SUCCESSFUL), ./gradlew :app:assembleDebug (BUILD SUCCESSFUL), then run-android.sh on the Temple tab in light and dark: the card matches the iOS after/ layout.",
                            "pr_title": "[TEMPLE-2210] Tighten the Temple reservation card",
                            "pr_body": PR_BODY1,
                            "screenshots": [{"path": f"{SCREENS}/reservation-card-light.png", "caption": "Reservation card, light"},
                                            {"path": f"{SCREENS}/reservation-card-dark.png", "caption": "Reservation card, dark"}],
                            "follow_ups": []}, ensure_ascii=False))
REFINE1 = ("Replaced the literal with the theme token and rebuilt.\n\n"
           + json.dumps({"outcome": "implemented",
                         "summary": "The Temple reservation card on Android now uses the tighter padding, spacing, and title style from iOS.",
                         "verification": "./gradlew :ui-templex:testDebugUnitTest and :app:assembleDebug pass after the change. Retook both screenshots on the emulator.",
                         "pr_title": "[TEMPLE-2210] Tighten the Temple reservation card",
                         "pr_body": PR_BODY1.replace("padding 16dp → 12dp", "padding `TempleSpacing.large` → `TempleSpacing.medium` (12dp)"),
                         "screenshots": [{"path": f"{SCREENS}/reservation-card-light.png", "caption": "Reservation card, light"},
                                         {"path": f"{SCREENS}/reservation-card-dark.png", "caption": "Reservation card, dark"}],
                         "follow_ups": []}, ensure_ascii=False))
REVIEWS1 = {
    "Your role is visual": "1. ui-templex/.../reservations/ReservationCard.kt:21 (medium): `Modifier.padding(12.dp)` hardcodes a dimension TempleSpacing already has as `medium`. A later spacing change to the theme would miss this card. Use `TempleSpacing.medium`.\n2. ReservationCard.kt:41 (low): iOS shows a plain borderless button. Consider an `OutlinedButton` so the destructive action reads as a button.",
    "Your role is conventions": "1. ReservationCard.kt:21: the module's CLAUDE.md says spacing comes from `TempleSpacing`, never a `.dp` literal. Use `TempleSpacing.medium` and drop the `unit.dp` import.",
    "Your role is simplicity": "No findings. The diff is three values, which is the smallest change that matches iOS.",
    "Your role is correctness": "No findings. `titleSmall` is the Material role for iOS subheadline semibold, and the card's layout is otherwise unchanged.",
}
DECIDE1 = {"summary": "One change: use the theme's spacing token for the card padding.",
           "changes": [{"file": A_CARD, "action": "Replace Modifier.padding(12.dp) with Modifier.padding(TempleSpacing.medium) and remove the androidx.compose.ui.unit.dp import.",
                        "backed_by": ["beauty", "conventions"]}],
           "conflicts": [{"topic": "Cancel button style",
                          "resolution": "Keep the TextButton in the error color. iOS shows a borderless destructive button, and an OutlinedButton grows the change past the iOS change.",
                          "overruled": ["beauty"]}]}
fakes1 = {
    "gh": [{"match": f"^pr view {N1} --repo fs-eng/mobile-ios --json number,title,url,state,isDraft",
            "stdout": ios_pr(N1, "TEMPLE-2210 Tighten the reservation card", CARD_BRANCH)}]
          + source_fakes(N1, CARD_BRANCH, CARD_CHANGES, BODY1)
          + [{"match": f"^pr create --draft --repo fs-eng/android-tree --base dev --head {CARD_BRANCH} ",
              "stdout": "https://github.com/fs-eng/android-tree/pull/9172\n"},
             {"match": "^pr comment https://github.com/fs-eng/android-tree/pull/9172 --body-file ",
              "stdout": "https://github.com/fs-eng/android-tree/pull/9172#issuecomment-2448810021\n"}],
    "curl": [{"match": "/rest/api/2/issue/TEMPLE-2210\\?fields=summary$", "stdout": ""}],
    "claude": [{"match": "--resume capture-session", "result": REFINE1}]
              + [{"match": role, "result": text} for role, text in REVIEWS1.items()]
              + [{"match": "You are porting one iOS pull request", "result": IMPLEMENT1}],
}
fx1 = {"description": "RyanPlumb's open mobile-ios pull request tightens the Temple reservation card. "
                      "prepare posts the Slack thread, source pulls the iOS diff and files, the session's port is "
                      "reviewed by four specialists, the lead keeps one finding, refine resumes the session, publish "
                      "opens a draft pull request, and screens posts the designer comment. setup is recorded "
                      "context: its worktree already holds the session's Android change, because a canned session "
                      "cannot edit files, so refine's resumed session leaves the same diff. The session runs on this "
                      "Mac (IOS_PORT_AWS=0), since the AWS session's files come back from S3. The two screenshots the "
                      "session names do not exist, because a fixture holds no binary PNG, so screens posts the "
                      "no-screenshots comment.",
       "source": "github", "env": dict(ENV), "files": files1, "repos": {"android-tree": ANDROID_REPO,
       IOS: ios_repo(CARD_BRANCH, "TEMPLE-2210 Tighten the reservation card", CARD_CHANGES)},
       "stages": ["prepare", "source", "implement", "review_assignments", "reviews", "decide", "refine", "publish", "screens"],
       "context": {**job_context(N1), "setup": SETUP1},
       "fakes": fakes1, "stubs": {"decide": DECIDE1}}
write(f"{D}/ported-draft-pr.json", fx1)

# ======================================================================== already-matches
N2 = 8857
ID2 = f"ios-{N2}"
PILL_CHANGES = {PILL: PILL_HEAD}
BODY2 = "The ordinance status pill uses the Temple accent color instead of system blue, so it matches dark mode."
IMPLEMENT2 = ("StatusPill in ui-templex already draws on MaterialTheme.colorScheme.primary, which the Temple theme maps "
              "to the same accent iOS now uses, in light and dark.\n\n"
              + json.dumps({"outcome": "already_fixed",
                            "summary": "Android's StatusPill already uses the theme's primary color, the Temple accent, so it matches the iOS change.",
                            "verification": "Read StatusPill.kt and the Temple color scheme, then run-android.sh on the Temple tab in light and dark: the pill is the accent color in both.",
                            "pr_title": "Use the Temple accent for the ordinance status pill",
                            "pr_body": "", "screenshots": [], "follow_ups": []}))
fakes2 = {
    "gh": [{"match": f"^pr view {N2} --repo fs-eng/mobile-ios --json number,title,url,state,isDraft",
            "stdout": ios_pr(N2, "Use the Temple accent for the ordinance status pill", PILL_BRANCH, state="MERGED")}]
          + source_fakes(N2, PILL_BRANCH, PILL_CHANGES, BODY2),
    "git": [{"match": "ls-remote --exit-code --heads origin ryan/ordinance-pill-accent", "exit": 2}],
    "claude": [{"match": "You are porting one iOS pull request", "result": IMPLEMENT2}],
}
fx2 = {"description": "RyanPlumb's merged mobile-ios pull request, with no Jira key, changes the status pill to the "
                      "Temple accent, which Android already uses. Every stage runs: setup makes the worktree, source "
                      "pulls the iOS diff, the session reports already_fixed, no review runs, the lead's stub decides "
                      "nothing, publish reports in Slack and removes the worktree, and screens finds no pull request. "
                      "The session runs on this Mac (IOS_PORT_AWS=0 with AWS_WORKER_FALLBACK=1).",
       "source": "github", "env": dict(ENV), "files": common_files(N2),
       "repos": {"android-tree": ANDROID_REPO, IOS: ios_repo(PILL_BRANCH, "Use the Temple accent for the status pill", PILL_CHANGES)},
       "context": job_context(N2), "fakes": fakes2,
       "stubs": {"decide": {"summary": "Nothing was implemented, so nothing changes.", "changes": [], "conflicts": []}}}
write(f"{D}/already-matches.json", fx2)

# ======================================================================== author-not-ported
N3 = 8862
fakes3 = {"gh": [{"match": f"^pr view {N3} --repo fs-eng/mobile-ios --json number,title,url,state,isDraft",
                  "stdout": ios_pr(N3, "TEMPLE-2219 Fix the reservation list's VoiceOver order", "TEMPLE-2219-voiceover-order",
                                   author="kjensen-fs") | {"headRefOid": "5f0c2e9a41d7b38c6e1f2a9d0b4c7e3f8a6d1b29",
                                                           "baseRefOid": "c81d4b7e2f9a0356d1e8b4c2a7f90e3d6b5a1c48"}}]}
fx3 = {"description": "A mobile-ios pull request by an engineer who is not in ios-port.json's authors. prepare declines "
                      "without posting to Slack, and every later stage passes the decline on: no worktree, no session, "
                      "no pull request.",
       "source": "github", "env": dict(ENV), "files": common_files(N3), "repos": {"android-tree": ANDROID_REPO},
       "context": job_context(N3), "fakes": fakes3,
       "stubs": {"decide": {"summary": "Nothing was implemented, so nothing changes.", "changes": [], "conflicts": []}}}
write(f"{D}/author-not-ported.json", fx3)
