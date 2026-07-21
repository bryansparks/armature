"""Ensure `import armature` resolves to THIS checkout's build, not the
editable-installed main-repo copy.

Without this, pytest run from `experiments/campaign/` (or even the repo root
without ``PYTHONPATH=.``) imports the editable-installed main-repo ``armature``
(e.g. commit 037a03d), which predates the memory-pyramid work and lacks
``_track_store`` / ``_knowledge_store`` / ``navigation_tools`` /
``curator_stage``. That silently makes Phase-3-behavior tests pass against a
build that has no Phase-3 code — false passes (the Task 6 smoke test was one:
it passed against main's build without ever exercising navigation).

Inserting this checkout's root at ``sys.path[0]`` makes ``import armature``
resolve here in EVERY checkout — the worktree gets its Phase-3 build, main
gets main's build. Same class of footgun as the ``armature`` console-script
resolution (eval Task 1 v2). Resolves relative to this file's location, so it
is correct regardless of which checkout the test suite runs from.
"""
import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))