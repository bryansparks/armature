import json
import os
from pathlib import Path

OPS = os.environ.get("OPS_CHECKOUT", "../armature-ops")

def roles():
    data = json.loads(Path(f"{OPS}/config/roles.json").read_text())
    data = {k: v for k, v in data.items() if not k.startswith("//")}
    for role in data["roles"]:
        role["platforms"] = [p for p in role["platforms"] if p != "dotnet"]
        role.get("models", {}).pop("dotnet", None)
    return data

def teams():
    data = json.loads(Path(f"{OPS}/config/teams.example.json").read_text())
    data["teams"] = [t for t in data["teams"] if t["id"] != "talentstorehouse"]
    for t in data["teams"]:
        t["repos"] = [r for r in t["repos"] if r["slug"].startswith("fs-eng/")]
    return data

KOTLIN_BASE = {
  "app/src/main/java/org/familysearch/tree/memories/MemoriesViewModel.kt":
    "package org.familysearch.tree.memories\n\nclass MemoriesViewModel(private val repo: MemoriesRepository) {\n    fun load(personId: String) = repo.memories(personId)\n}\n",
  "app/src/main/java/org/familysearch/tree/memories/MemoriesRepository.kt":
    "package org.familysearch.tree.memories\n\ninterface MemoriesRepository {\n    fun memories(personId: String): List<Memory>\n}\n",
}
KOTLIN_CHANGE = {
  "app/src/main/java/org/familysearch/tree/memories/MemoriesViewModel.kt":
    "package org.familysearch.tree.memories\n\nclass MemoriesViewModel(private val repo: MemoriesRepository) {\n    private var cache: Map<String, List<Memory>> = emptyMap()\n\n    fun load(personId: String): List<Memory> =\n        cache[personId] ?: repo.memories(personId).also { cache = cache + (personId to it) }\n\n    fun signedOut() {\n        cache = emptyMap()\n    }\n}\n",
}

def write(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")

def neutral(data):
    """Ops config with this machine's paths replaced by neutral ones."""
    text = json.dumps(data).replace("/Users/stephan/Library/Android/sdk", "/opt/android-sdk") \
        .replace("/Users/stephan/projects/familysearch/", "/srv/")
    return json.loads(text)
