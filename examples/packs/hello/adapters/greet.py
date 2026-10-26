import json
import os

params = json.loads(os.environ["ARMATURE_WITH"])
print(json.dumps({"greeting": f"Hello, {params['name']}!", "pack_dir": os.environ["PACK_DIR"]}))
