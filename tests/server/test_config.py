import pytest
from pydantic import ValidationError

from armature.server.config import ServerConfig, load_config


def test_defaults_are_loopback():
    cfg = ServerConfig()
    assert (cfg.host, cfg.port, cfg.base_path) == ("127.0.0.1", 8098, "")


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5", "example.com"])
def test_public_bind_refused(host):
    with pytest.raises(ValidationError, match="allow_public_bind"):
        ServerConfig(host=host)


def test_public_bind_allowed_explicitly():
    assert ServerConfig(host="0.0.0.0", allow_public_bind=True).host == "0.0.0.0"


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_loopback_hosts(host):
    assert ServerConfig(host=host).host == host


def test_yaml_with_env_overrides(tmp_path):
    path = tmp_path / "server.yaml"
    path.write_text("database_url: sqlite:///a.db\nhost: 0.0.0.0\nallow_public_bind: true\n"
                    "oidc:\n  issuer: https://okta.example\n  client_id: abc\n  client_secret_ref: OIDC_SECRET\n"
                    "  admin_groups: [armature-admins]\n  group_roles:\n    mobile-devs: [{scope_kind: team, scope: mobile, role: contributor}]\n")
    cfg = load_config(path, env={"ARMATURE_SERVER_DATABASE_URL": "sqlite:///b.db", "ARMATURE_SERVER_PORT": "9000"})
    assert cfg.database_url == "sqlite:///b.db"
    assert cfg.port == 9000
    assert cfg.oidc.group_roles["mobile-devs"][0].role == "contributor"


def test_env_cannot_force_public_bind_without_flag(tmp_path):
    with pytest.raises(ValidationError):
        load_config(tmp_path / "absent.yaml", env={"ARMATURE_SERVER_HOST": "0.0.0.0"})


def test_config_path_from_env(tmp_path):
    path = tmp_path / "custom.yaml"
    path.write_text("port: 8100\n")
    assert load_config(env={"ARMATURE_SERVER_CONFIG": str(path)}).port == 8100


@pytest.mark.parametrize("field,value", [
    ("master_key", "plain"), ("master_key", "vault:x"), ("base_path", "dash/"), ("base_path", "/dash/"),
])
def test_bad_values_refused(field, value):
    with pytest.raises(ValidationError):
        ServerConfig(**{field: value})


def test_unknown_role_in_group_roles_refused():
    with pytest.raises(ValidationError):
        ServerConfig(oidc={"issuer": "i", "client_id": "c", "client_secret_ref": "s", "group_roles": {"g": [{"scope_kind": "team", "scope": "t", "role": "root"}]}})
