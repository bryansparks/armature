import base64
import os

import pytest

from armature.server.crypto import (
    Keyring,
    MasterKeyError,
    SealError,
    generate_master_key,
    key_id_of,
    load_master_key,
    write_key_file,
)

AAD = {"team_id": "t1", "secret_id": "s1", "name": "GH_TOKEN"}


def test_round_trip(keyring):
    sealed = keyring.seal("hunter2", AAD)
    assert sealed.startswith(f"v1:{keyring.key_id}:")
    assert "hunter2" not in sealed
    assert keyring.open_(sealed, AAD) == "hunter2"


def test_key_order_in_aad_does_not_matter(keyring):
    sealed = keyring.seal("x", AAD)
    assert keyring.open_(sealed, dict(reversed(list(AAD.items())))) == "x"


def test_key_id_is_sha256_prefix():
    master = b"m" * 32
    assert Keyring(master).key_id == key_id_of(master) and len(key_id_of(master)) == 8


@pytest.mark.parametrize("other", [
    {"team_id": "t2", "secret_id": "s1", "name": "GH_TOKEN"},
    {"team_id": "t1", "secret_id": "s2", "name": "GH_TOKEN"},
    {"team_id": "t1", "secret_id": "s1", "name": "OTHER"},
    {"team_id": None, "secret_id": "s1", "name": "GH_TOKEN"},
])
def test_ciphertext_moved_to_another_row_fails(keyring, other):
    sealed = keyring.seal("hunter2", AAD)
    with pytest.raises(SealError):
        keyring.open_(sealed, other)


def test_tampered_byte_fails(keyring):
    prefix, body = keyring.seal("hunter2", AAD).rsplit(":", 1)
    raw = bytearray(base64.b64decode(body))
    for index in (0, 12, len(raw) - 1):
        flipped = bytearray(raw)
        flipped[index] ^= 1
        with pytest.raises(SealError):
            keyring.open_(f"{prefix}:{base64.b64encode(bytes(flipped)).decode()}", AAD)


@pytest.mark.parametrize("sealed", ["", "v1", "v2:abcd1234:AAAA", "v1:abcd1234:not base64!", "v1:{kid}:AAAA"])
def test_malformed_values_fail_closed(keyring, sealed):
    with pytest.raises(SealError):
        keyring.open_(sealed.format(kid=keyring.key_id), AAD)


def test_wrong_master_key_fails(keyring):
    sealed = keyring.seal("hunter2", AAD)
    with pytest.raises(SealError):
        Keyring(b"z" * 32).open_(sealed, AAD)
    same_id = sealed.replace(keyring.key_id, Keyring(b"z" * 32).key_id)
    with pytest.raises(SealError):
        Keyring(b"z" * 32).open_(same_id, AAD)


def test_labels_use_separate_keys(keyring):
    sealed = keyring.seal("x", AAD, label="totp")
    with pytest.raises(SealError):
        keyring.open_(sealed, AAD, label="secrets")
    assert len({keyring.subkey(label) for label in ("secrets", "sessions", "totp")}) == 3


def test_key_file_round_trip(tmp_path):
    path = tmp_path / "master.key"
    write_key_file(path)
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert len(load_master_key(f"file:{path}")) == 32


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_key_file_readable_by_others_refused(tmp_path, mode):
    path = tmp_path / "master.key"
    write_key_file(path)
    os.chmod(path, mode)
    with pytest.raises(MasterKeyError, match="chmod 600"):
        load_master_key(f"file:{path}")


def test_symlinked_key_file_refused(tmp_path):
    real = tmp_path / "real.key"
    write_key_file(real)
    link = tmp_path / "link.key"
    link.symlink_to(real)
    with pytest.raises(MasterKeyError):
        load_master_key(f"file:{link}")


def test_fifo_key_file_refused(tmp_path):
    fifo = tmp_path / "fifo.key"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(MasterKeyError, match="regular file"):
        load_master_key(f"file:{fifo}")


def test_missing_key_fails_closed(tmp_path):
    with pytest.raises(MasterKeyError):
        load_master_key(f"file:{tmp_path / 'absent.key'}")
    with pytest.raises(MasterKeyError):
        load_master_key("env:ARMATURE_MASTER_KEY", env={})


def test_env_key(tmp_path):
    key = generate_master_key()
    assert load_master_key("env:ARMATURE_MASTER_KEY", env={"ARMATURE_MASTER_KEY": key}) == base64.b64decode(key)


@pytest.mark.parametrize("value", ["not-base64!", base64.b64encode(b"short").decode()])
def test_malformed_key_refused(value):
    with pytest.raises(MasterKeyError):
        load_master_key("env:K", env={"K": value})


def test_kms_is_an_interface_only():
    with pytest.raises(NotImplementedError):
        load_master_key("kms:arn:aws:kms:us-east-1:123:key/abc")
