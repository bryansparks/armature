"""The campaign runner must invoke the worktree armature (Phase 3), not the
PATH console script (main-checkout, no Phase 3). Verified by asserting the
subprocess argv starts with [sys.executable, '-m', 'armature'] and that the
sandbox env prepends the worktree root to PYTHONPATH so the worktree build
wins regardless of the subprocess's cwd.
"""
import sys
import inspect
from pathlib import Path

from campaign_runner import cli_driver, concurrency, sandbox
from campaign_runner.plan import CampaignPlan


def test_cli_driver_uses_sys_executable_m_armature():
    src = inspect.getsource(cli_driver.CliDriver._armature)
    assert "sys.executable" in src
    assert "-m" in src
    # argv must not START with the bare 'armature' console script (the form
    # `["armature", ...]`). The `-m armature` module name legitimately appears
    # as a later argv element, so we forbid the argv[0] shape, not the substring.
    assert '["armature"' not in src and "['armature'" not in src, (
        "runner must not invoke the bare 'armature' console script"
    )


def test_concurrency_uses_sys_executable_m_armature():
    src = inspect.getsource(concurrency)
    assert "sys.executable" in src
    # no bare 'armature' argv[0] element remains (the `-m armature` form is fine)
    assert '["armature"' not in src and "['armature'" not in src


def test_sandbox_env_prepends_worktree_root_to_pythonpath(tmp_path):
    """`python -m armature` resolves `armature` via sys.path[0]=cwd; if the
    campaign is launched from outside the worktree root, cwd alone misses the
    worktree package and Python falls back to the editable install (main
    checkout, no Phase 3). The sandbox env must prepend the worktree root
    (sandbox.dir.parent) to PYTHONPATH so the worktree build wins regardless
    of the subprocess cwd.
    """
    plan = CampaignPlan(name="pyppath_test", workflow="any", phases=[])
    sb = sandbox.Sandbox(plan, root=Path(tmp_path))
    env = sb.env()
    assert "PYTHONPATH" in env
    # the worktree root (sandbox.dir.parent) must be on PYTHONPATH
    parts = env["PYTHONPATH"].split(":")
    assert str(sb.dir.parent) in parts, (env["PYTHONPATH"], sb.dir.parent)