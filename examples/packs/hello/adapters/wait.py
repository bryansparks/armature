import json
import os
import time

seconds = json.loads(os.environ["ARMATURE_WITH"])["seconds"]
time.sleep(seconds)
print(json.dumps({"waited": seconds}))
