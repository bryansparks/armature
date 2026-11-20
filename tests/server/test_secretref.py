"""Secret references: env and file built in, anything else through the configured provider."""
import os
import traceback

import pytest

from armature.server.crypto import MasterKeyError, generate_master_key, load_master_key
from armature.server.secretref import SecretRefError, resolve

SECRET = "s3cret-value"
PROVIDER = "tests.server.test_secretref:provide"
MASTER = generate_master_key()
calls = []


def provide(ref):
    calls.append(ref)
    return SECRET if ref == "vault:db" else MASTER


def explode(ref):
    raise RuntimeError(f"backend said {SECRET}")


def nothing(ref):
    return ""


def test_env():
    assert resolve("env:PGPASSWORD", env={"PGPASSWORD": SECRET}) == SECRET
    with pytest.raises(SecretRefError, match="PGPASSWORD is not set"):
        resolve("env:PGPASSWORD", env={})


def test_file_drops_the_trailing_newline(tmp_path):
    path = tmp_path / "db.pass"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, f"{SECRET}\n".encode())
    os.close(fd)
    assert resolve(f"file:{path}") == SECRET


def test_file_readable_by_others_refused(tmp_path):
    path = tmp_path / "db.pass"
    path.write_text(SECRET)
    path.chmod(0o644)
    with pytest.raises(SecretRefError, match="chmod 600") as caught:
        resolve(f"file:{path}")
    assert SECRET not in str(caught.value)


def test_symlinked_file_refused(tmp_path):
    real = tmp_path / "real.pass"
    fd = os.open(real, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    link = tmp_path / "link.pass"
    link.symlink_to(real)
    with pytest.raises(SecretRefError):
        resolve(f"file:{link}")


def test_provider_gets_the_whole_reference():
    calls.clear()
    assert resolve("vault:db", provider=PROVIDER) == SECRET
    assert calls == ["vault:db"]


def test_unknown_scheme_without_a_provider_names_the_scheme():
    with pytest.raises(SecretRefError, match="vault: references") as caught:
        resolve("vault:db")
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("provider", ["tests.server.test_secretref:explode", "tests.server.test_secretref:nothing"])
def test_a_failing_provider_never_shows_the_value(provider):
    with pytest.raises(SecretRefError) as caught:
        resolve("vault:db", provider=provider)
    assert SECRET not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("provider", ["tests.server.absent:fetch", "tests.server.test_secretref:absent", "nope"])
def test_a_provider_that_cannot_load_is_an_error(provider):
    with pytest.raises(SecretRefError):
        resolve("vault:db", provider=provider)


@pytest.mark.parametrize("ref", ["plain", "env:", ":x", "Vault:x"])
def test_malformed_references_refused(ref):
    with pytest.raises(SecretRefError):
        resolve(ref, provider=PROVIDER)


def test_master_key_through_a_provider():
    assert len(load_master_key("vault:master", provider=PROVIDER)) == 32


def test_master_key_provider_scheme_without_a_provider_refused():
    with pytest.raises(MasterKeyError):
        load_master_key("vault:master")


def test_kms_master_key_goes_to_the_provider():
    calls.clear()
    assert len(load_master_key("kms:arn:aws:kms:us-east-1:123:key/abc", provider=PROVIDER)) == 32
    assert calls == ["kms:arn:aws:kms:us-east-1:123:key/abc"]


def test_kms_master_key_without_a_provider_stays_unimplemented():
    with pytest.raises(NotImplementedError):
        load_master_key("kms:arn:aws:kms:us-east-1:123:key/abc")
