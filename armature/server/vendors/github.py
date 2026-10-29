"""GitHub connections: the App JWT, Verify, and installation tokens cached in memory only.

A connection whose config says ``auth: gh_cli`` instead uses the token the
``gh`` CLI holds for the server's OS user. That token is never stored or revoked.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import threading
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from armature.server.vendors import VendorError, Verified, call, client, json_of

API = "https://api.github.com"
REQUIRED = {"contents": "write", "pull_requests": "write", "issues": "write", "checks": "write", "metadata": "read"}
LEVEL = {"read": 1, "write": 2, "admin": 3}
MARGIN = timedelta(minutes=5)
PAGE = 100
MAX_PAGES = 10
GH_CLI = "gh_cli"
GH_HOST = "github.com"
GH_TIMEOUT = 10
GH_TTL = timedelta(minutes=3)

_tokens: dict[tuple[Any, ...], tuple[str, datetime]] = {}
_lock = threading.Lock()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def load_key(private_key_pem: str) -> rsa.RSAPrivateKey:
    try:
        key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    except (ValueError, TypeError) as exc:
        raise VendorError("The private key is not a readable PEM key.") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise VendorError("The private key is not an RSA key.")
    return key


def app_jwt(private_key_pem: str, issuer: str, now: datetime) -> str:
    """An RS256 App JWT, backdated a minute for clock drift and good for nine more."""
    stamp = int(now.timestamp())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64(json.dumps({"iat": stamp - 60, "exp": stamp + 540, "iss": issuer}, separators=(",", ":")).encode())
    signing = f"{header}.{claims}".encode()
    signature = load_key(private_key_pem).sign(signing, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{claims}.{_b64(signature)}"


def headers(token: str | None = None) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def uses_gh_cli(config: dict[str, Any] | None) -> bool:
    return (config or {}).get("auth") == GH_CLI


def gh_api(host: str | None) -> str:
    """The REST base for a gh host, never taken from the caller, so the gh token only goes to that host."""
    return API if host in (None, GH_HOST) else f"https://{host}/api/v3"


def _run_gh(host: str) -> str:
    gh = shutil.which("gh")
    if gh is None:
        raise VendorError("The gh CLI is not installed for the server's user.")
    try:
        done = subprocess.run([gh, "auth", "token", "--hostname", host], capture_output=True, text=True,
                              timeout=GH_TIMEOUT, check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise VendorError("gh auth token timed out.") from None
    except OSError:
        raise VendorError("gh could not be run.") from None
    if done.returncode != 0:
        raise VendorError(f"gh has no login for {host}. Run gh auth login as the server's user.")
    return done.stdout.strip()


# Tests replace this, so no test runs gh.
run_gh = _run_gh


def gh_token(config: dict[str, Any], now: datetime, *, fresh: bool = False) -> str:
    """The gh CLI's token for the configured host, reused for a few minutes. Never stored."""
    host = config.get("host") or GH_HOST
    key = (GH_CLI, host)
    with _lock:
        hit = None if fresh else _tokens.get(key)
        if hit is not None and hit[1] > now:
            return hit[0]
    token = run_gh(host)
    if not token or not token.isprintable() or any(c.isspace() for c in token):
        raise VendorError("gh auth token gave no token.")
    with _lock:
        _tokens[key] = (token, now + GH_TTL)
    return token


def _issuer(config: dict[str, Any]) -> str:
    return str(config.get("client_id") or config.get("app_id"))


def missing(permissions: dict[str, Any] | None) -> list[str]:
    have = permissions or {}
    return [f"{name}: {level}" for name, level in REQUIRED.items()
            if LEVEL.get(str(have.get(name)), 0) < LEVEL[level]]


def _verify_gh_cli(config: dict[str, Any], now: datetime, transport: httpx.BaseTransport | None) -> Verified:
    base = gh_api(config.get("host"))
    token = gh_token(config, now, fresh=True)
    with client(transport) as http:
        response = call(http, "GET", f"{base}/user", "GitHub GET /user", headers=headers(token))
    user = json_of(response, "GitHub GET /user")
    login = user.get("login") if isinstance(user, dict) else None
    if not isinstance(login, str) or not login:
        raise VendorError("GitHub GET /user named no user.")
    problems = []
    scopes = response.headers.get("X-OAuth-Scopes")
    found = sorted({s.strip() for s in scopes.split(",") if s.strip()}) if scopes is not None else None
    if found is not None and "repo" not in found:
        problems.append("The gh login lacks the repo scope. Run gh auth refresh -s repo as the server's user.")
    identity = {"auth": GH_CLI, "login": login, "user_id": user.get("id"), "scopes": found}
    return Verified(identity=identity, problems=problems)


def verify(creds: dict[str, str], config: dict[str, Any], *, now: datetime,
           transport: httpx.BaseTransport | None = None) -> Verified:
    if uses_gh_cli(config):
        return _verify_gh_cli(config, now, transport)
    base = config.get("api_base") or API
    jwt = app_jwt(creds["private_key_pem"], _issuer(config), now)
    with client(transport) as http:
        app = json_of(call(http, "GET", f"{base}/app", "GitHub GET /app", headers=headers(jwt)), "GitHub GET /app")
        slug = app.get("slug") if isinstance(app, dict) else None
        if not isinstance(slug, str) or not slug:
            raise VendorError("GitHub GET /app named no app.")
        login = f"{slug}[bot]"
        bot = json_of(call(http, "GET", f"{base}/users/{quote(login)}", "GitHub GET /users", headers=headers()),
                      "GitHub GET /users")
        found, truncated = [], True
        for page in range(1, MAX_PAGES + 1):
            batch = json_of(call(http, "GET", f"{base}/app/installations", "GitHub GET /app/installations",
                                 headers=headers(jwt), params={"per_page": PAGE, "page": page}),
                            "GitHub GET /app/installations")
            if not isinstance(batch, list):
                raise VendorError("GitHub GET /app/installations did not answer with a list.")
            found += batch
            if len(batch) < PAGE:
                truncated = False
                break
    installations, problems = [], []
    if truncated:
        problems.append(f"The app has more than {PAGE * MAX_PAGES} installations. Only the first "
                        f"{PAGE * MAX_PAGES} were recorded.")
    for item in found:
        account = item.get("account") if isinstance(item, dict) else None
        owner = account.get("login") if isinstance(account, dict) else None
        if not isinstance(owner, str) or not isinstance(item.get("id"), int) \
                or owner.lower() in {i["owner"] for i in installations}:
            continue
        permissions = item.get("permissions") if isinstance(item.get("permissions"), dict) else {}
        installations.append({"owner": owner.lower(), "installation_id": item["id"], "permissions": permissions})
        lacking = missing(permissions)
        if lacking:
            problems.append(f"The installation on {owner} lacks {', '.join(lacking)}.")
    if not installations:
        problems.append("The app has no installations. Install it on an organization or account.")
    identity = {"slug": slug, "bot_login": login, "bot_user_id": bot.get("id") if isinstance(bot, dict) else None,
                "app_id": app.get("id")}
    return Verified(identity=identity, problems=problems, installations=installations)


def mint(*, connection_id: str, creds: dict[str, str], config: dict[str, Any], installation_id: int | None,
         repo: str, permissions: dict[str, str], now: datetime, transport: httpx.BaseTransport | None = None,
         cache: bool = True) -> str:
    """A token for one repository and permission set, reused until five minutes before it expires. Never stored.

    Without ``cache`` the token is new and never reused, so its holder can revoke it alone. A gh CLI connection
    gets the gh login's token whatever the repository or permissions, and it must never be revoked.
    """
    if uses_gh_cli(config):
        return gh_token(config, now)
    if installation_id is None:
        raise VendorError("The GitHub App has no installation for this repository.")
    key = (connection_id, installation_id, repo, tuple(sorted(permissions.items())))
    with _lock:
        hit = _tokens.get(key) if cache else None
        if hit is not None and hit[1] - MARGIN > now:
            return hit[0]
    base = config.get("api_base") or API
    jwt = app_jwt(creds["private_key_pem"], _issuer(config), now)
    what = "GitHub POST /app/installations/access_tokens"
    with client(transport) as http:
        made = json_of(call(http, "POST", f"{base}/app/installations/{int(installation_id)}/access_tokens", what,
                            headers=headers(jwt), json={"repositories": [repo], "permissions": permissions}), what)
    token, expires = (made.get("token"), made.get("expires_at")) if isinstance(made, dict) else (None, None)
    try:
        expires_at = datetime.fromisoformat(str(expires))
    except ValueError as exc:
        raise VendorError(f"{what} gave no expiry.") from exc
    if not isinstance(token, str) or not token:
        raise VendorError(f"{what} gave no token.")
    if cache:
        with _lock:
            _tokens[key] = (token, expires_at)
    return token


def revoke(api_base: str, token: str, transport: httpx.BaseTransport | None = None) -> None:
    what = "GitHub DELETE /installation/token"
    with client(transport) as http:
        response = call(http, "DELETE", f"{api_base}/installation/token", what, headers=headers(token))
    if response.status_code not in (204, 401):
        raise VendorError(f"{what} answered HTTP {response.status_code}.")


def forget(connection_id: str) -> None:
    with _lock:
        for key in [k for k in _tokens if k[0] == connection_id]:
            del _tokens[key]
