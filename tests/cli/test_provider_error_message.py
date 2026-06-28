from armature.cli import _print_provider_error


class _CreditsErr(Exception):
    status_code = 402

    def __init__(self):
        super().__init__("insufficient credits")


class _AuthErr(Exception):
    status_code = 401

    def __init__(self):
        super().__init__("invalid api key")


class _WeirdErr(Exception):
    pass


def test_credits_message_printed(capsys):
    assert _print_provider_error(_CreditsErr()) is True
    err = capsys.readouterr().err.lower()
    assert "credits" in err
    assert "402" in err


def test_auth_message_still_handled(capsys):
    assert _print_provider_error(_AuthErr()) is True
    err = capsys.readouterr().err.lower()
    assert "api key" in err


def test_unknown_error_returns_false():
    assert _print_provider_error(_WeirdErr("x")) is False
