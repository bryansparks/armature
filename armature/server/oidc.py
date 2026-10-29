"""OpenID Connect sign-in: authorization code with PKCE, ID token checks, and group roles.

Accounts link on (iss, sub) only, never on email. Every sign-in recomputes the
user's OIDC memberships and admin flag from the configured groups, so a group
removed at the provider revokes what it granted.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime
from typing import Any

import httpx
from authlib.common.security import generate_token
from authlib.common.urls import add_params_to_uri
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from sqlalchemy import Connection, and_, delete, insert, select, update

from armature.server import audit
from armature.server.config import OIDCConfig
from armature.server.db import new_id
from armature.server.db.schema import memberships, projects, sessions, teams, users
from armature.server.rbac import higher
from armature.server.users import USERNAME, active_admin_count

STATE_TTL = 600
LEEWAY = 60
FRESH_AUTH = 300
ASYMMETRIC = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"})
DEFAULT_ALG = {("RSA", None): "RS256", ("EC", "P-256"): "ES256", ("EC", "P-384"): "ES384",
               ("EC", "P-521"): "ES512", ("OKP", "Ed25519"): "EdDSA"}


class OIDCError(Exception):
    """Sign-in failed. The message is for the log, never for the browser."""


def sign_state(key: bytes, payload: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    mac = hmac.new(key, b"oidc-state." + body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def open_state(key: bytes, value: str | None, now: float) -> dict[str, Any]:
    if not value or "." not in value:
        raise OIDCError("state cookie missing")
    body, mac = value.rsplit(".", 1)
    expected = hmac.new(key, b"oidc-state." + body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        raise OIDCError("state cookie signature mismatch")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    if not isinstance(payload, dict) or payload.get("exp", 0) < now:
        raise OIDCError("state cookie expired")
    return payload


class Provider:
    """One configured issuer. Discovery and keys are fetched lazily and cached."""

    def __init__(self, cfg: OIDCConfig, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self._transport = transport
        self._metadata: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None

    def _client(self) -> httpx.Client:
        return httpx.Client(transport=self._transport, timeout=10.0, follow_redirects=False)

    def metadata(self) -> dict[str, Any]:
        if self._metadata is None:
            url = self.cfg.issuer.rstrip("/") + "/.well-known/openid-configuration"
            with self._client() as client:
                response = client.get(url)
            response.raise_for_status()
            data = response.json()
            if data.get("issuer") != self.cfg.issuer:
                raise OIDCError(f"discovery names issuer {data.get('issuer')!r}, not {self.cfg.issuer!r}")
            self._metadata = data
        return self._metadata

    def jwks(self, refresh: bool = False) -> dict[str, Any]:
        if self._jwks is None or refresh:
            with self._client() as client:
                response = client.get(self.metadata()["jwks_uri"])
            response.raise_for_status()
            self._jwks = response.json()
        return self._jwks

    def authorization_url(self, *, redirect_uri: str, state: str, nonce: str, verifier: str,
                          fresh: bool = False) -> str:
        params = [("response_type", "code"), ("client_id", self.cfg.client_id), ("redirect_uri", redirect_uri),
                  ("scope", " ".join(self.cfg.scopes)), ("state", state), ("nonce", nonce),
                  ("code_challenge", create_s256_code_challenge(verifier)), ("code_challenge_method", "S256")]
        if fresh:
            params += [("prompt", "login"), ("max_age", "0")]
        return add_params_to_uri(self.metadata()["authorization_endpoint"], params)

    def exchange(self, *, code: str, redirect_uri: str, verifier: str, client_secret: str) -> dict[str, Any]:
        with self._client() as client:
            response = client.post(self.metadata()["token_endpoint"], data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            }, auth=(self.cfg.client_id, client_secret), headers={"Accept": "application/json"})
        if response.status_code != 200:
            raise OIDCError(f"token endpoint answered {response.status_code}")
        token = response.json()
        if not isinstance(token, dict) or not isinstance(token.get("id_token"), str):
            raise OIDCError("token response has no id_token")
        return token

    def _algorithms(self, jwks: dict[str, Any]) -> set[str]:
        algs = set()
        for key in jwks.get("keys", []):
            alg = key.get("alg") or DEFAULT_ALG.get((key.get("kty"), key.get("crv")))
            if alg in ASYMMETRIC and key.get("use", "sig") == "sig":
                algs.add(alg)
        return algs

    def verify_id_token(self, id_token: str, *, nonce: str, now: datetime, fresh: bool = False) -> dict[str, Any]:
        """Check the token. With ``fresh``, the provider must say the user signed in within the last five minutes."""
        header = _header(id_token)
        jwks = self.jwks()
        if header.get("kid") and header["kid"] not in {k.get("kid") for k in jwks.get("keys", [])}:
            jwks = self.jwks(refresh=True)
        algorithms = self._algorithms(jwks)
        if header.get("alg") not in algorithms:
            raise OIDCError(f"ID token alg {header.get('alg')!r} is not one the provider's keys use")
        try:
            token = jwt.decode(id_token, KeySet.import_key_set(jwks), algorithms=sorted(algorithms))
            jwt.JWTClaimsRegistry(
                now=int(now.timestamp()), leeway=LEEWAY,
                iss={"essential": True, "value": self.cfg.issuer},
                aud={"essential": True, "value": self.cfg.client_id},
                exp={"essential": True}, sub={"essential": True},
                nonce={"essential": True, "value": nonce},
            ).validate(token.claims)
        except (JoseError, ValueError) as exc:
            raise OIDCError(f"ID token refused: {exc}") from exc
        claims = token.claims
        aud = claims.get("aud")
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != self.cfg.client_id:
            raise OIDCError("ID token has several audiences and azp is not this client")
        if fresh:
            auth_time = claims.get("auth_time")
            stamp = int(now.timestamp())
            if not isinstance(auth_time, int) or not stamp - FRESH_AUTH <= auth_time <= stamp + LEEWAY:
                raise OIDCError("step-up sign-in has no recent auth_time")
        return claims


def _header(token: str) -> dict[str, Any]:
    try:
        head = token.split(".", 1)[0]
        return json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))
    except (ValueError, IndexError) as exc:
        raise OIDCError("ID token is malformed") from exc


def new_flow() -> dict[str, str]:
    return {"state": generate_token(32), "nonce": generate_token(32), "verifier": generate_token(64)}


def groups_of(claims: dict[str, Any], claim: str) -> set[str]:
    value = claims.get(claim)
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {g for g in value if isinstance(g, str)}
    return set()


def _resolve_scope(conn: Connection, scope_kind: str, scope: str) -> str | None:
    if scope_kind == "team":
        return conn.execute(select(teams.c.id).where(teams.c.name == scope)).scalar_one_or_none()
    team_name, _, project_name = scope.partition("/")
    return conn.execute(
        select(projects.c.id).select_from(projects.join(teams, teams.c.id == projects.c.team_id))
        .where(teams.c.name == team_name, projects.c.name == project_name)).scalar_one_or_none()


def granted_roles(conn: Connection, cfg: OIDCConfig, groups: set[str]) -> dict[tuple[str, str], str]:
    """(scope_kind, scope_id) to the highest role the user's groups grant there. Unknown groups grant nothing."""
    grants: dict[tuple[str, str], str] = {}
    for group in sorted(groups):
        for grant in cfg.group_roles.get(group, []):
            scope_id = _resolve_scope(conn, grant.scope_kind, grant.scope)
            if scope_id is not None:
                key = (grant.scope_kind, scope_id)
                grants[key] = higher(grants.get(key), grant.role)
    return grants


def sync_memberships(conn: Connection, user_id: str, grants: dict[tuple[str, str], str], now: datetime) -> bool:
    """Replace the user's OIDC memberships with ``grants``. Manual memberships stay. Returns whether any changed."""
    current = {(r.scope_kind, r.scope_id): (r.role, r.source) for r in conn.execute(
        select(memberships.c.scope_kind, memberships.c.scope_id, memberships.c.role, memberships.c.source)
        .where(memberships.c.user_id == user_id))}
    oidc_now = {key: role for key, (role, source) in current.items() if source == "oidc"}
    wanted = {key: role for key, role in grants.items() if current.get(key, (None, "oidc"))[1] == "oidc"}
    if oidc_now == wanted:
        return False
    conn.execute(delete(memberships).where(memberships.c.user_id == user_id, memberships.c.source == "oidc"))
    for (scope_kind, scope_id), role in wanted.items():
        conn.execute(insert(memberships).values(id=new_id(), user_id=user_id, scope_kind=scope_kind,
                                                scope_id=scope_id, role=role, source="oidc", created_at=now))
    return True


def sign_in(conn: Connection, cfg: OIDCConfig, claims: dict[str, Any], *, now: datetime, ip: str | None,
            allow_username: bool = True) -> str:
    """Find or create the user for these claims, refresh roles, and return the user id."""
    iss, sub = claims["iss"], str(claims["sub"])
    groups = groups_of(claims, cfg.groups_claim)
    is_admin = bool(groups & set(cfg.admin_groups))
    row = conn.execute(select(users).where(and_(users.c.oidc_iss == iss, users.c.oidc_sub == sub))).first()
    display = _display_name(claims)
    email = claims.get("email") if isinstance(claims.get("email"), str) else None
    if row is None:
        user_id = new_id()
        conn.execute(insert(users).values(
            id=user_id, username=_free_username(conn, claims) if allow_username else None, display_name=display, email=(email or "")[:191] or None,
            oidc_iss=iss, oidc_sub=sub, password_hash=None, totp_secret_enc=None, last_totp_step=0,
            is_admin=is_admin, disabled=False, created_at=now, last_login_at=now))
        audit.record(conn, action="user.create_oidc", actor_id=user_id, target_kind="user", target_id=user_id,
                     detail={"iss": iss, "admin": is_admin}, ip=ip)
        admin_changed = False
    else:
        if row.disabled:
            raise OIDCError("user is disabled")
        user_id = row.id
        conn.execute(update(users).where(users.c.id == user_id).values(
            is_admin=is_admin, display_name=display, email=(email or "")[:191] or None, last_login_at=now))
        # Losing the admin group must not leave the instance with no admin at all, so the last one keeps it.
        if row.is_admin and not is_admin and active_admin_count(conn) == 0:
            conn.execute(update(users).where(users.c.id == user_id).values(is_admin=True))
            audit.record(conn, action="oidc.last_admin_kept", actor_id=None, target_kind="user", target_id=user_id,
                         detail={"iss": iss}, ip=ip)
            is_admin = True
        admin_changed = bool(row.is_admin) != is_admin
        if admin_changed:
            audit.record(conn, action="user.update", actor_id=None, target_kind="user", target_id=user_id,
                         detail={"is_admin": {"old": bool(row.is_admin), "new": is_admin}, "source": "oidc"}, ip=ip)
    roles_changed = sync_memberships(conn, user_id, granted_roles(conn, cfg, groups), now)
    if admin_changed or roles_changed:
        conn.execute(delete(sessions).where(sessions.c.user_id == user_id))
    return user_id


def _display_name(claims: dict[str, Any]) -> str | None:
    for key in ("name", "preferred_username", "email"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:191]
    return None


def _free_username(conn: Connection, claims: dict[str, Any]) -> str | None:
    """The provider's preferred username when it is valid and unused, else none. Never a link to a local account."""
    wanted = claims.get("preferred_username")
    if not isinstance(wanted, str):
        return None
    wanted = wanted.strip().lower()
    if not USERNAME.fullmatch(wanted):
        return None
    taken = conn.execute(select(users.c.id).where(users.c.username == wanted)).first()
    return None if taken else wanted
