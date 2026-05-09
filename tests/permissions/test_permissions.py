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


def test_classify_cat_as_readonly():
    assert classify_shell_command("cat /etc/hosts") == PermissionLevel.READ_ONLY


def test_classify_echo_as_readonly():
    assert classify_shell_command("echo hello world") == PermissionLevel.READ_ONLY


def test_classify_git_log_as_readonly():
    assert classify_shell_command("git log --oneline -10") == PermissionLevel.READ_ONLY


def test_classify_git_diff_as_readonly():
    assert classify_shell_command("git diff HEAD~1") == PermissionLevel.READ_ONLY


def test_classify_kill_as_destructive():
    assert classify_shell_command("kill -9 1234") == PermissionLevel.DESTRUCTIVE


def test_classify_chmod_777_as_destructive():
    assert classify_shell_command("chmod 777 /tmp/file") == PermissionLevel.DESTRUCTIVE


def test_classify_unknown_command_as_workspace():
    assert classify_shell_command("python script.py") == PermissionLevel.WORKSPACE


def test_classify_npm_install_as_workspace():
    assert classify_shell_command("npm install") == PermissionLevel.WORKSPACE


def test_requires_approval_workspace_false():
    assert requires_approval(PermissionLevel.WORKSPACE) is False


def test_requires_approval_network_false():
    assert requires_approval(PermissionLevel.NETWORK) is False


def test_classify_strips_leading_whitespace():
    assert classify_shell_command("  rm -rf /tmp") == PermissionLevel.DESTRUCTIVE


def test_classify_find_as_readonly():
    assert classify_shell_command("find . -name '*.py'") == PermissionLevel.READ_ONLY
