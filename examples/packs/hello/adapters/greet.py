import json
import os

raw = os.environ.get("ARMATURE_WITH")
if raw is None:
    with open(os.environ["ARMATURE_WITH_FILE"], encoding="utf-8") as f:
        raw = f.read()
params = json.loads(raw)
print(json.dumps({"greeting": f"Hello, {params['name']}!", "pack_dir": os.environ["PACK_DIR"]}))
