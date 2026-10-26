"""scrypt password hashes, in the dashboard's format so its hashes import as they are."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

N = 2 ** 15
R = 8
P = 1
KEYLEN = 64
PASSWORD_MIN = 12
PASSWORD_MAX = 200


def _maxmem(cost: int, block_size: int) -> int:
    return 128 * cost * block_size * 2


# The cost travels in the stored hash, so tests can use a cheaper one without weakening production.
def hash_password(password: str, *, cost: int = N) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=cost, r=R, p=P, dklen=KEYLEN,
                         maxmem=_maxmem(cost, R))
    return f"scrypt${cost}${R}${P}${base64.b64encode(salt).decode()}${base64.b64encode(key).decode()}"


def verify_password(password: str, stored: str) -> bool:
    parts = str(stored).split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        cost, block_size, parallelism = int(parts[1]), int(parts[2]), int(parts[3])
        salt, expected = base64.b64decode(parts[4]), base64.b64decode(parts[5])
        derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=cost, r=block_size, p=parallelism,
                                 dklen=len(expected), maxmem=_maxmem(cost, block_size))
    except (ValueError, OverflowError):
        return False
    return hmac.compare_digest(derived, expected)


def check_password_policy(password: str) -> None:
    if len(password) < PASSWORD_MIN:
        raise ValueError(f"password must be at least {PASSWORD_MIN} characters")
    if len(password) > PASSWORD_MAX:
        raise ValueError(f"password must be at most {PASSWORD_MAX} characters")
