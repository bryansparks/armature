import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from fx_common import *

D = __file__.rsplit("/", 2)[0] + "/repo-review"

REVIEWS = {
 "correctness": "1. MemoriesViewModel.kt:7 (high, confident): the cache is keyed by person id only, so a different signed-in user sees the previous user's memories until signedOut() runs. signedOut() is never called from the sign-out flow.",
 "simplicity": "No findings. The cache is small and local.",
 "test_adequacy": "1. MemoriesViewModel.kt:9 (medium): signedOut() has no test, so nothing pins the cache clearing that the sign-out defect depends on.",
}


def claude_answers():
    out = [{"match": f"prompts/{role}\\.md|\\n\\n[^\\n]*{role}", "result": text} for role, text in REVIEWS.items()]
    return out + [{"match": "", "result": "No findings."}]


def repo(changed):
    return {"base": "dev", "remote": "fs-eng/android-tree", "files": KOTLIN_BASE,
            "commits": ([{"message": "Cache memories per person", "files": KOTLIN_CHANGE}] if changed else [])}


lead_changes = {"summary": "One high-severity change in android-tree: the new memories cache leaks across sign-out.",
  "changes": [{"repo": "fs-eng/android-tree", "file": "app/src/main/java/org/familysearch/tree/memories/MemoriesViewModel.kt",
               "action": "Clear the memories cache from the sign-out flow and key it by user as well as person.",
               "severity": "high", "backed_by": ["correctness", "test_adequacy"]}],
  "conflicts": [], "no_action": ["simplicity found nothing to change."]}
digest = "*Daily review:* fs-eng/android-tree (1 commit) and fs-eng/mobile-api-nodejs (no new commits).\n*1 high-severity change*\n• android-tree `MemoriesViewModel.kt`: clear the memories cache on sign-out and key it by user.\nReviewed android-tree; mobile-api-nodejs had nothing new."
quiet_lead = {"summary": "Nothing landed since the last review.", "changes": [], "conflicts": [], "no_action": []}
quiet_digest = "*Daily review:* no new commits on fs-eng/android-tree or fs-eng/mobile-api-nodejs. Nothing to review."


def fixture(desc, changed, lead, dig):
    t = teams()
    for team in t["teams"]:
        for r in team["repos"]:
            r["pipelines"]["daily_review"] = r["slug"] in ("fs-eng/android-tree", "fs-eng/mobile-api-nodejs")
    api = {"base": "master", "remote": "fs-eng/mobile-api-nodejs",
           "files": {"src/routes/memories.js": "module.exports = (req, res) => res.json([])\n"}}
    return {"description": desc, "source": "schedule", "env": {"REVIEW_FIRST_RUN_WINDOW": "1 day ago"},
            "files": {"config/teams.json": t, "config/roles.json": roles(),
                      "state/marks/fs-eng_android-tree": "{{rev:state/repos/fs-eng_android-tree:dev~" + ("1" if changed else "0") + "}}",
                      "state/marks/fs-eng_mobile-api-nodejs": "{{rev:state/repos/fs-eng_mobile-api-nodejs:master}}"},
            "repos": {"state/repos/fs-eng_android-tree": repo(changed), "state/repos/fs-eng_mobile-api-nodejs": api},
            "fakes": {"claude": claude_answers()},
            "stubs": {"team_lead": lead, "digest": {"content": dig}}}


write(f"{D}/one-repo-changed.json", fixture("Android landed one commit and the API none: android's nine roles review it, the lead keeps one high change.", True, lead_changes, digest))
write(f"{D}/nothing-landed.json", fixture("No watched repo moved since its mark, so no session runs and the lead decides nothing.", False, quiet_lead, quiet_digest))
