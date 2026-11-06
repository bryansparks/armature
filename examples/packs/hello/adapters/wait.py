import json
import os
import time

raw = os.environ.get("ARMATURE_WITH")
if raw is None:
    with open(os.environ["ARMATURE_WITH_FILE"], encoding="utf-8") as f:
        raw = f.read()
seconds = json.loads(raw)["seconds"]
time.sleep(seconds)
print(json.dumps({"waited": seconds}))
