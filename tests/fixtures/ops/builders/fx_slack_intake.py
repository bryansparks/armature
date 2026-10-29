import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from fx_common import write

D = __file__.rsplit("/", 2)[0] + "/slack-intake"


def event(text, ts, user="U04KXN2QJ7M", **extra):
    return {"token": "verification-token-unused", "team_id": "T024BE7LD", "api_app_id": "A07INTAKE1",
            "type": "event_callback", "event_id": f"Ev07{ts.replace('.', '')[:10]}", "event_time": int(float(ts)),
            "event": {"type": "message", "channel": "C07MOBINTAKE", "channel_type": "channel", "user": user,
                      "text": text, "ts": ts, "event_ts": ts, **extra}}


headers = {"content-type": "application/json", "user-agent": "Slackbot 1.0 (+https://api.slack.com/robots)"}
env = {"SLACK_INTAKE_CHANNEL": "C07MOBINTAKE", "JIRA_PROJECT": "MEMTEMDIS",
       "JIRA_SERVER": "https://familysearch.atlassian.net"}
cases = {
 "create-defect": ("A reproducible defect with no ticket becomes a new Defect, and the thread gets its link.",
   event("On Android 6.14 beta the Memories tab is blank after you sign out and back in. Kill the app and it comes back.", "1761675401.204519"),
   {"intent": "create", "confidence": 0.92, "summary": "Fix blank Memories tab after signing out and back in on Android",
    "description": "On Android 6.14 beta, the Memories tab is blank after signing out and signing back in. Force-quitting the app restores it.\n\nOriginal message:\n> On Android 6.14 beta the Memories tab is blank after you sign out and back in. Kill the app and it comes back.",
    "issue_type": "Defect", "issue_key": ""},
   {"jira": [{"match": "^issue create", "stdout": "✓ Issue created\nhttps://familysearch.atlassian.net/browse/MEMTEMDIS-4801\n"}]}),
 "comment-on-existing": ("A message naming an existing key adds its detail as a comment there.",
   event("MEMTEMDIS-4620 also happens on iPad when you rotate the device while the tab loads.", "1761676022.551209"),
   {"intent": "comment", "confidence": 0.88, "summary": "Memories tab blank on iPad after rotation",
    "description": "Also reproduces on iPad when the device rotates while the Memories tab loads.\n\nOriginal message:\n> MEMTEMDIS-4620 also happens on iPad when you rotate the device while the tab loads.",
    "issue_type": "Defect", "issue_key": "MEMTEMDIS-4620"},
   {"jira": [{"match": "^issue comment add MEMTEMDIS-4620 ", "stdout": "✓ Comment added to issue \"MEMTEMDIS-4620\"\n"}]}),
 "ignore-question": ("A question is ignored: no Jira call and no reply.",
   event("Does anyone know when the 6.14 beta goes out to Play?", "1761676544.000400"),
   {"intent": "ignore", "confidence": 0.95, "summary": "", "description": "", "issue_type": "Task", "issue_key": ""},
   {}),
 "low-confidence-create": ("A create below 0.7 confidence does nothing, the threshold standing in for a gate.",
   event("the tree view feels slow lately?", "1761677102.118200"),
   {"intent": "create", "confidence": 0.55, "summary": "Investigate slow tree view",
    "description": "Reporter says the tree view feels slow.", "issue_type": "Task", "issue_key": ""},
   {}),
}
for name, (desc, payload, classify, fakes) in cases.items():
    write(f"{D}/{name}.json", {"description": desc, "source": "slack", "headers": headers, "env": env,
                               "payload": payload, "stubs": {"classify": classify}, "fakes": fakes})
