"""Master key loading and AES-256-GCM sealing for values stored in the database.

A sealed value is ``v1:<key_id>:<base64(nonce | ciphertext+tag)>``. The key id
is the first 8 hex characters of SHA-256 over the master key, so a rotation can
tell which key sealed a value. Every value binds caller-supplied additional
data, so a ciphertext copied into another row fails to open.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets as _random
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = "v1"
KEY_BYTES = 32
NONCE_BYTES = 12
LABELS = ("secrets", "sessions", "totp", "runners")


class MasterKeyError(Exception):
    """The master key is missing, malformed, or stored unsafely."""


class SealError(Exception):
    """A sealed value could not be opened."""


def generate_master_key() -> str:
    return base64.b64encode(_random.token_bytes(KEY_BYTES)).decode("ascii")


def write_key_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as out:
        out.write(generate_master_key() + "\n")


def _decode_key(text: str, where: str) -> bytes:
    try:
        key = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MasterKeyError(f"master key in {where} is not base64") from exc
    if len(key) != KEY_BYTES:
        raise MasterKeyError(f"master key in {where} must decode to {KEY_BYTES} bytes, got {len(key)}")
    return key


def load_master_key(source: str, env: Mapping[str, str] | None = None) -> bytes:
    env = os.environ if env is None else env
    scheme, _, rest = source.partition(":")
    if scheme == "env":
        value = env.get(rest)
        if not value:
            raise MasterKeyError(f"master key variable {rest} is not set")
        return _decode_key(value, f"${rest}")
    if scheme == "file":
        path = Path(rest).expanduser()
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError as exc:
            raise MasterKeyError(f"master key file {path} does not exist") from exc
        except OSError as exc:
            raise MasterKeyError(f"master key file {path} can't be opened: {exc.strerror}") from exc
        with os.fdopen(fd, "r", encoding="ascii") as handle:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise MasterKeyError(f"master key file {path} is not a regular file")
            if info.st_mode & 0o077:
                raise MasterKeyError(
                    f"master key file {path} has mode {stat.S_IMODE(info.st_mode):04o}; chmod 600 it")
            if info.st_uid != os.getuid():
                raise MasterKeyError(f"master key file {path} is not owned by this user")
            return _decode_key(handle.read(), str(path))
    if scheme == "kms":
        raise NotImplementedError("kms master keys are not implemented yet")
    raise MasterKeyError(f"unknown master key source {source!r}")


def key_id_of(master: bytes) -> str:
    return hashlib.sha256(master).hexdigest()[:8]


def _aad_bytes(aad: Mapping[str, Any]) -> bytes:
    return json.dumps(aad, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class Keyring:
    """Sub-keys derived from one master key with HKDF-SHA256, one per label."""

    def __init__(self, master: bytes):
        if len(master) != KEY_BYTES:
            raise MasterKeyError(f"master key must be {KEY_BYTES} bytes")
        self.key_id = key_id_of(master)
        self._keys = {label: _derive(master, label) for label in LABELS}

    def subkey(self, label: str) -> bytes:
        return self._keys[label]

    def seal(self, plaintext: str, aad: Mapping[str, Any], label: str = "secrets") -> str:
        nonce = _random.token_bytes(NONCE_BYTES)
        ct = AESGCM(self._keys[label]).encrypt(nonce, plaintext.encode("utf-8"), _aad_bytes(aad))
        return f"{VERSION}:{self.key_id}:{base64.b64encode(nonce + ct).decode('ascii')}"

    def open_(self, sealed: str, aad: Mapping[str, Any], label: str = "secrets") -> str:
        try:
            version, key_id, body = str(sealed).split(":", 2)
            if version != VERSION:
                raise SealError(f"unknown sealed value version {version!r}")
            if key_id != self.key_id:
                raise SealError(f"value was sealed under key {key_id}, not {self.key_id}")
            raw = base64.b64decode(body, validate=True)
            if len(raw) <= NONCE_BYTES:
                raise SealError("sealed value is truncated")
            plain = AESGCM(self._keys[label]).decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], _aad_bytes(aad))
            return plain.decode("utf-8")
        except SealError:
            raise
        except (InvalidTag, ValueError, binascii.Error, UnicodeDecodeError, KeyError) as exc:
            raise SealError("sealed value failed to open") from exc


def _derive(master: bytes, label: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None,
                info=f"armature-server {label} v1".encode("ascii")).derive(master)


def keyring_from_source(source: str, env: Mapping[str, str] | None = None) -> Keyring:
    return Keyring(load_master_key(source, env))
