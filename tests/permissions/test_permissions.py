from armature.permissions.permissions import (
    classify_shell_command, PermissionLevel, requires_approval
)


# ── Reversibility enum ─────────────────────────────────────────────────────────

def test_reversibility_full_value():
    from armature.permissions.permissions import Reversibility
    assert Reversibility.FULL.value == "full"


def test_reversibility_partial_value():
    from armature.permissions.permissions import Reversibility
    assert Reversibility.PARTIAL.value == "partial"


def test_reversibility_none_value():
    from armature.permissions.permissions import Reversibility
    assert Reversibility.NONE.value == "none"


def test_reversibility_is_str_enum():
    from armature.permissions.permissions import Reversibility
    assert isinstance(Reversibility.FULL, str)
    assert isinstance(Reversibility.PARTIAL, str)
    assert isinstance(Reversibility.NONE, str)


def test_reversibility_has_three_members():
    from armature.permissions.permissions import Reversibility
    assert len(list(Reversibility)) == 3


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


# ── Indirection / chaining bypasses (Claim 3) ──────────────────────────────────
# A denylist that only inspects the leading token is bypassable. The classifier
# must tokenize the command, inspect every subcommand in a chain, strip leading
# env assignments, normalize absolute paths to their basename, and recurse one
# level into `sh -c`/`bash -c` indirection.

def test_classify_sh_c_indirection_is_destructive():
    assert classify_shell_command('sh -c "rm -rf /tmp/old"') == PermissionLevel.DESTRUCTIVE


def test_classify_bash_c_indirection_is_destructive():
    assert classify_shell_command('bash -c "rm -rf /tmp/old"') == PermissionLevel.DESTRUCTIVE


def test_classify_chained_echo_then_rm_is_destructive():
    assert classify_shell_command("echo hi; rm -rf /tmp/old") == PermissionLevel.DESTRUCTIVE


def test_classify_and_chain_with_rm_is_destructive():
    assert classify_shell_command("true && rm -rf /tmp/old") == PermissionLevel.DESTRUCTIVE


def test_classify_or_chain_with_rm_is_destructive():
    assert classify_shell_command("false || rm -rf /tmp/old") == PermissionLevel.DESTRUCTIVE


def test_classify_pipe_chain_with_rm_is_destructive():
    assert classify_shell_command("echo hi | rm -rf /tmp/old") == PermissionLevel.DESTRUCTIVE


def test_classify_env_prefix_rm_is_destructive():
    assert classify_shell_command("FOO=bar rm -rf /tmp/old") == PermissionLevel.DESTRUCTIVE


def test_classify_absolute_path_rm_is_destructive():
    assert classify_shell_command("/bin/rm -rf /tmp/old") == PermissionLevel.DESTRUCTIVE


def test_classify_absolute_path_sudo_is_destructive():
    assert classify_shell_command("/usr/bin/sudo apt install curl") == PermissionLevel.DESTRUCTIVE


def test_classify_sh_c_nested_chain_is_destructive():
    assert classify_shell_command('sh -c "echo hi; rm -rf /tmp/old"') == PermissionLevel.DESTRUCTIVE


def test_classify_chained_readonly_then_workspace_is_workspace():
    # most-dangerous-wins: a workspace command in the chain upgrades the result
    assert classify_shell_command("echo hi; python script.py") == PermissionLevel.WORKSPACE


def test_classify_chained_all_readonly_is_readonly():
    assert classify_shell_command("echo hi; ls -la /tmp") == PermissionLevel.READ_ONLY


def test_classify_chained_destructive_short_circuits():
    # destructive anywhere in the chain -> destructive
    assert classify_shell_command("ls -la /tmp; rm -rf /tmp/old; echo done") == PermissionLevel.DESTRUCTIVE


def test_classify_env_prefix_does_not_swallow_readonly():
    assert classify_shell_command("FOO=bar ls -la /tmp") == PermissionLevel.READ_ONLY


def test_classify_empty_command_is_workspace():
    assert classify_shell_command("") == PermissionLevel.WORKSPACE
    assert classify_shell_command("   ") == PermissionLevel.WORKSPACE


def test_classify_trailing_separator_is_destructive():
    assert classify_shell_command("rm -rf /tmp/old;") == PermissionLevel.DESTRUCTIVE
