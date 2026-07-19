"""Module entry point so `python -m armature` works.

The `armature` console script on PATH resolves to the main-checkout install
(no Phase 3 code). Running `python -m armature` from the worktree root puts
cwd on sys.path[0], so it imports THIS worktree's armature — the build that
has the memory pyramid. The campaign runner invokes this form (see
cli_driver.py / concurrency.py).
"""
from armature.cli import app

if __name__ == "__main__":
    app()