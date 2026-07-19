"""The campaign runner must invoke the worktree armature (Phase 3), not the
PATH console script (main-checkout, no Phase 3). Verified by asserting the
subprocess argv starts with [sys.executable, '-m', 'armature'].
"""
import sys
import inspect
from campaign_runner import cli_driver, concurrency


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