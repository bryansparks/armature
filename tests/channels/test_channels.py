"""RED tests for Messaging Channel Connectors feature.

All production imports are deferred inside each test function so that
collection succeeds even before armature/channels/ exists, and failures
are reported as individual test failures rather than collection errors.
"""
import pytest
import pydantic
from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_SPEC = {
    "name": "my-channel",
    "channels": [
        {
            "name": "support-bot",
            "platform": "telegram",
            "token": "abc123",
            "routes": [
                {"pattern": ".*", "workflow": "support.yaml"},
            ],
        }
    ],
}

TWO_CHANNEL_SPEC = {
    "name": "multi-channel",
    "channels": [
        {
            "name": "support-bot",
            "platform": "telegram",
            "token": "tok-telegram",
            "routes": [
                {"pattern": "help", "workflow": "help.yaml"},
                {"pattern": ".*", "workflow": "support.yaml"},
            ],
        },
        {
            "name": "slack-alerts",
            "platform": "slack",
            "token": "tok-slack",
            "signing_secret": "slack-secret",
            "routes": [
                {"pattern": "alert", "workflow": "alert.yaml"},
                {"pattern": ".*", "workflow": "default.yaml"},
            ],
        },
    ],
}


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

def test_channel_route_model_exists_with_required_fields():
    """ChannelRoute must have pattern and workflow fields."""
    from armature.channels.models import ChannelRoute

    route = ChannelRoute(pattern=".*", workflow="support.yaml")
    assert route.pattern == ".*"
    assert route.workflow == "support.yaml"


def test_channel_config_model_exists_with_required_fields():
    """ChannelConfig must have name, platform, token, and routes fields."""
    from armature.channels.models import ChannelConfig, ChannelRoute

    config = ChannelConfig(
        name="support-bot",
        platform="telegram",
        token="tok-abc",
        routes=[ChannelRoute(pattern=".*", workflow="support.yaml")],
    )
    assert config.name == "support-bot"
    assert config.platform == "telegram"
    assert config.token == "tok-abc"
    assert len(config.routes) == 1


def test_channel_spec_model_exists_with_required_fields():
    """ChannelSpec must have name and channels fields."""
    from armature.channels.models import ChannelSpec, ChannelConfig, ChannelRoute

    spec = ChannelSpec(
        name="my-channel",
        channels=[
            ChannelConfig(
                name="support-bot",
                platform="telegram",
                token="tok-abc",
                routes=[ChannelRoute(pattern=".*", workflow="support.yaml")],
            )
        ],
    )
    assert spec.name == "my-channel"
    assert len(spec.channels) == 1


def test_channel_config_invalid_platform_raises_validation_error():
    """Platform must be 'telegram' or 'slack'; any other value raises ValidationError."""
    from armature.channels.models import ChannelConfig, ChannelRoute

    with pytest.raises(pydantic.ValidationError):
        ChannelConfig(
            name="bad-bot",
            platform="discord",  # not a valid Literal
            token="tok-xyz",
            routes=[ChannelRoute(pattern=".*", workflow="noop.yaml")],
        )


def test_channel_spec_round_trips_from_dict():
    """ChannelSpec can be constructed from a plain dict (YAML-loaded data)."""
    from armature.channels.models import ChannelSpec

    spec = ChannelSpec(**MINIMAL_SPEC)
    assert spec.name == "my-channel"
    assert spec.channels[0].platform == "telegram"
    assert spec.channels[0].routes[0].workflow == "support.yaml"


def test_channel_config_signing_secret_is_optional():
    """signing_secret defaults to None and is not required for any platform."""
    from armature.channels.models import ChannelConfig, ChannelRoute

    config = ChannelConfig(
        name="no-secret-bot",
        platform="slack",
        token="tok-slack",
        routes=[ChannelRoute(pattern=".*", workflow="default.yaml")],
    )
    assert config.signing_secret is None


# ---------------------------------------------------------------------------
# Router tests
# ---------------------------------------------------------------------------

def test_message_router_is_importable():
    """MessageRouter class must exist in armature.channels.router."""
    from armature.channels.router import MessageRouter  # noqa: F401


def test_find_workflow_returns_workflow_for_exact_match():
    """find_workflow returns the workflow path when pattern matches via re.search."""
    from armature.channels.models import ChannelSpec
    from armature.channels.router import MessageRouter

    spec = ChannelSpec(**{
        "name": "test",
        "channels": [
            {
                "name": "support-bot",
                "platform": "telegram",
                "token": "tok",
                "routes": [
                    {"pattern": "help", "workflow": "help.yaml"},
                ],
            }
        ],
    })
    router = MessageRouter(spec)
    result = router.find_workflow("support-bot", "help")
    assert result == "help.yaml"


def test_find_workflow_returns_workflow_for_regex_pattern():
    """find_workflow uses re.search so partial and regex patterns match correctly."""
    from armature.channels.models import ChannelSpec
    from armature.channels.router import MessageRouter

    spec = ChannelSpec(**{
        "name": "test",
        "channels": [
            {
                "name": "support-bot",
                "platform": "telegram",
                "token": "tok",
                "routes": [
                    {"pattern": r"\d{3}", "workflow": "numbers.yaml"},
                ],
            }
        ],
    })
    router = MessageRouter(spec)
    result = router.find_workflow("support-bot", "call me at 555 please")
    assert result == "numbers.yaml"


def test_find_workflow_returns_first_match_when_multiple_routes_exist():
    """When multiple patterns match, find_workflow returns the first one in list order."""
    from armature.channels.models import ChannelSpec
    from armature.channels.router import MessageRouter

    spec = ChannelSpec(**{
        "name": "test",
        "channels": [
            {
                "name": "support-bot",
                "platform": "telegram",
                "token": "tok",
                "routes": [
                    {"pattern": "help", "workflow": "help.yaml"},
                    {"pattern": ".*", "workflow": "fallback.yaml"},
                ],
            }
        ],
    })
    router = MessageRouter(spec)
    # "help" matches both routes; should return the first
    result = router.find_workflow("support-bot", "help")
    assert result == "help.yaml"


def test_find_workflow_returns_none_when_no_pattern_matches():
    """find_workflow returns None when no route pattern matches the message text."""
    from armature.channels.models import ChannelSpec
    from armature.channels.router import MessageRouter

    spec = ChannelSpec(**{
        "name": "test",
        "channels": [
            {
                "name": "support-bot",
                "platform": "telegram",
                "token": "tok",
                "routes": [
                    {"pattern": "^ONLY_THIS$", "workflow": "exact.yaml"},
                ],
            }
        ],
    })
    router = MessageRouter(spec)
    result = router.find_workflow("support-bot", "something completely different")
    assert result is None


def test_find_workflow_scopes_to_correct_channel():
    """Routes from one channel must not bleed into another channel's lookup."""
    from armature.channels.models import ChannelSpec
    from armature.channels.router import MessageRouter

    spec = ChannelSpec(**TWO_CHANNEL_SPEC)
    router = MessageRouter(spec)

    # "alert" only exists in slack-alerts routes, not support-bot
    telegram_result = router.find_workflow("support-bot", "alert")
    slack_result = router.find_workflow("slack-alerts", "alert")

    # telegram support-bot has no "alert" pattern as a first route; it has
    # "help" then ".*" — so "alert" matches the catch-all ".*" -> support.yaml
    assert telegram_result == "support.yaml"
    # slack-alerts has an explicit "alert" pattern first
    assert slack_result == "alert.yaml"


def test_find_workflow_catch_all_pattern_matches_any_text():
    """A route with pattern '.*' should match any non-empty and empty text."""
    from armature.channels.models import ChannelSpec
    from armature.channels.router import MessageRouter

    spec = ChannelSpec(**MINIMAL_SPEC)
    router = MessageRouter(spec)

    assert router.find_workflow("support-bot", "literally anything") == "support.yaml"
    assert router.find_workflow("support-bot", "12345!@#") == "support.yaml"
    assert router.find_workflow("support-bot", "") == "support.yaml"


def test_find_workflow_returns_none_for_unknown_channel():
    """find_workflow returns None when the channel_name does not exist in the spec."""
    from armature.channels.models import ChannelSpec
    from armature.channels.router import MessageRouter

    spec = ChannelSpec(**MINIMAL_SPEC)
    router = MessageRouter(spec)

    result = router.find_workflow("nonexistent-channel", "hello")
    assert result is None


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

def test_channels_start_command_exists():
    """'armature channels start' must exist as a registered command (not exit code 2)."""
    from typer.testing import CliRunner
    from armature.cli import app

    runner = CliRunner()
    # Invoke with --help to avoid needing a real file; exit 2 = "No such command"
    result = runner.invoke(app, ["channels", "start", "--help"])
    assert result.exit_code != 2, (
        f"'channels start' command not registered — got exit code 2.\nOutput: {result.output}"
    )


def test_channels_start_with_valid_spec_exits_0(tmp_path):
    """'armature channels start <valid_spec>' should exit 0 when the spec is valid."""
    import yaml
    from typer.testing import CliRunner
    from armature.cli import app

    spec_file = tmp_path / "channel.yaml"
    spec_file.write_text(yaml.dump(MINIMAL_SPEC))

    runner = CliRunner()
    result = runner.invoke(app, ["channels", "start", str(spec_file)])
    assert result.exit_code == 0, (
        f"Expected exit 0 for valid spec, got {result.exit_code}.\nOutput: {result.output}"
    )


def test_channels_start_with_missing_file_exits_1(tmp_path):
    """'armature channels start <nonexistent_file>' should exit 1."""
    from typer.testing import CliRunner
    from armature.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["channels", "start", str(tmp_path / "does_not_exist.yaml")])
    assert result.exit_code == 1, (
        f"Expected exit 1 for missing spec file, got {result.exit_code}.\nOutput: {result.output}"
    )


def test_channels_start_with_invalid_platform_exits_1(tmp_path):
    """'armature channels start' should exit 1 when the YAML fails model validation."""
    import yaml
    from typer.testing import CliRunner
    from armature.cli import app

    bad_spec = {
        "name": "bad-channel",
        "channels": [
            {
                "name": "my-bot",
                "platform": "discord",  # invalid platform
                "token": "tok",
                "routes": [{"pattern": ".*", "workflow": "noop.yaml"}],
            }
        ],
    }
    spec_file = tmp_path / "bad_channel.yaml"
    spec_file.write_text(yaml.dump(bad_spec))

    runner = CliRunner()
    result = runner.invoke(app, ["channels", "start", str(spec_file)])
    assert result.exit_code == 1, (
        f"Expected exit 1 for invalid spec, got {result.exit_code}.\nOutput: {result.output}"
    )
