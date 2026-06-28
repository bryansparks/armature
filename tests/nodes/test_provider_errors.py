from armature.nodes.provider_errors import classify_provider_error, is_account_scoped


class _Err(Exception):
    def __init__(self, msg="", status_code=None):
        super().__init__(msg)
        self.status_code = status_code


def test_401_status_is_auth():
    assert classify_provider_error(_Err("nope", status_code=401)) == "provider_auth"


def test_402_status_is_credits():
    assert classify_provider_error(_Err("insufficient credits", status_code=402)) == "provider_credits"


def test_402_message_only_is_credits():
    assert classify_provider_error(_Err("insufficient credits")) == "provider_credits"


def test_402_code_in_message_is_credits():
    assert classify_provider_error(_Err('{"error": {"code": 402, "message": "out of credit"}}')) == "provider_credits"


def test_403_key_revoked_is_auth():
    assert classify_provider_error(_Err("Invalid API key revoked", status_code=403)) == "provider_auth"


def test_403_content_policy_is_none():
    assert classify_provider_error(_Err("content policy violation", status_code=403)) is None


def test_429_is_none():
    assert classify_provider_error(_Err("rate limit", status_code=429)) is None


def test_500_is_none():
    assert classify_provider_error(_Err("boom", status_code=500)) is None


def test_bare_exception_is_none():
    assert classify_provider_error(Exception("anything")) is None


def test_auth_class_with_key_message_is_auth():
    class AuthenticationError(Exception):
        pass
    assert classify_provider_error(AuthenticationError("invalid api key")) == "provider_auth"


def test_auth_class_without_key_message_is_none():
    class AuthenticationError(Exception):
        pass
    assert classify_provider_error(AuthenticationError("some other auth issue")) is None


def test_is_account_scoped_wrapper():
    assert is_account_scoped(_Err(status_code=402)) is True
    assert is_account_scoped(_Err(status_code=401)) is True
    assert is_account_scoped(_Err(status_code=500)) is False
    assert is_account_scoped(Exception("x")) is False
