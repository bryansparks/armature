from armature.permissions.permissions import (
    classify_shell_command, PermissionLevel, requires_approval
)

def test_classify_ls_as_readonly():
    assert classify_shell_command("ls -la /tmp") == PermissionLevel.READ_ONLY

def test_classify_grep_as_readonly():
    assert classify_shell_command("grep -r 'pattern' .") == PermissionLevel.READ_ONLY

def test_classify_rm_as_destructive():
    assert classify_shell_command("rm -rf /tmp/old") == PermissionLevel.DESTRUCTIVE

def test_classify_sudo_as_destructive():
    assert classify_shell_command("sudo apt install curl") == PermissionLevel.DESTRUCTIVE

def test_classify_git_commit_as_workspace():
    assert classify_shell_command("git commit -m 'msg'") == PermissionLevel.WORKSPACE

def test_requires_approval_for_destructive():
    assert requires_approval(PermissionLevel.DESTRUCTIVE) is True

def test_no_approval_for_readonly():
    assert requires_approval(PermissionLevel.READ_ONLY) is False
