"""GitHub's GraphQL and REST APIs for the sync service: read-only, and polite to the token that live reviews share.

Calls pause while fewer than FLOOR points remain, until the window resets, and pause for as long as a 403 or 429
rate limit says. Other repositories on the same token pause with them, without calling. Tokens are never logged.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Self

import httpx
from sqlalchemy import Connection, select

from armature.server import connections
from armature.server.crypto import Keyring, SealError
from armature.server.db.schema import github_installations
from armature.server.insights import repos
from armature.server.tools import Call
from armature.server.vendors import VendorError, client
from armature.server.vendors import call as vendor_call
from armature.server.vendors import github as vendor

PERSONAL = "This repository needs an admin to approve syncing with a personal token."
READ = {"pull_requests": "read", "contents": "read", "checks": "read", "metadata": "read"}
FLOOR = 1500
RETRIES = 3
BACKOFF_S = 1.0
SECONDARY = timedelta(minutes=1)
TIMEOUT = 30.0
RETRY = (502, 503)
LIMITED = (403, 429)
GRAPHQL, CORE = "graphql", "core"
# mergeStateStatus still sits behind this preview.
GRAPHQL_ACCEPT = "application/vnd.github.merge-info-preview+json"

_paused: dict[tuple[Any, ...], datetime] = {}
_lock = threading.Lock()


class GitHubError(Exception):
    """A GitHub call failed. The message is safe to show and store."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


class GraphQLError(GitHubError):
    def __init__(self, message: str, types: tuple[str, ...] = ()):
        super().__init__(message)
        self.types = types


class Paused(Exception):
    """The token's rate limit is spent for now. Try again at ``until``."""

    def __init__(self, until: datetime):
        super().__init__(f"Paused for GitHub's rate limit until {until.isoformat()}.")
        self.until = until


@dataclass(frozen=True)
class Answer:
    data: Any
    remaining: int | None = None
    reset_at: datetime | None = None

    @property
    def pause_until(self) -> datetime | None:
        """When to call again, if this answer left too few points for live reviews."""
        return self.reset_at if self.remaining is not None and self.remaining < FLOOR else None


def call_for(conn: Connection, keyring: Keyring, repo: Any, now: datetime,
             transport: httpx.BaseTransport | None) -> Call:
    """A call for a tracked repository without a run: its pinned connection, else the team's for its owner."""
    owner = repo.full_name.split("/")[0]
    try:
        row = repos.connection_for(conn, repo.team_id, repo.connection_id, repo.project_id, owner)
    except repos.RepoError as exc:
        raise GitHubError(exc.message, exc.status) from exc
    if not repos.may_sync(conn, repo, row):
        raise GitHubError(PERSONAL, 403)
    try:
        creds = connections.credentials(keyring, row)
    except (SealError, ValueError) as exc:
        raise GitHubError(f"The credentials of connection {row.name} can't be opened.") from exc
    installation = conn.execute(select(github_installations.c.installation_id).where(
        github_installations.c.connection_id == row.id, github_installations.c.owner == owner.lower())).scalar()
    return Call(run_id="", step_id="", connection=row, creds=creds, now=now, installation_id=installation,
                project_id=repo.project_id, transport=transport, team_id=repo.team_id)


def _base(config: dict[str, Any]) -> str:
    if config.get("api_base"):
        return str(config["api_base"]).rstrip("/")
    return vendor.gh_api(config.get("host")) if vendor.uses_gh_cli(config) else vendor.API


def graphql_url(base: str) -> str:
    """GitHub's GraphQL endpoint beside a REST base: /graphql on github.com, /api/graphql on GitHub Enterprise."""
    return f"{base[:-len('/v3')]}/graphql" if base.endswith("/api/v3") else f"{base}/graphql"


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _time(value: Any) -> datetime | None:
    try:
        found = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return found.astimezone(UTC) if found is not None and found.tzinfo else None


def _epoch(value: str | None) -> datetime | None:
    return datetime.fromtimestamp(int(value), UTC) if value and value.strip().isdigit() else None


def paused_until(key: tuple[Any, ...], now: datetime) -> datetime | None:
    with _lock:
        until = _paused.get(key)
    return until if until is not None and until > now else None


class Client:
    """GitHub calls for one tracked repository as its connection, with read permissions only."""

    def __init__(self, call: Call, repo: str, *, clock: Callable[[], datetime],
                 sleep: Callable[[float], None] = time.sleep):
        self.call = call
        self.repo = repo
        self.base = _base(call.config)
        self.graphql_url = graphql_url(self.base)
        self.halvings = 0
        self._in_page = 0
        self._clock = clock
        self._sleep = sleep
        self._token: str | None = None
        self._http = client(call.transport, timeout=TIMEOUT)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self._http.close()

    def token(self) -> str:
        if self._token is None:
            if self.call.installation_id is None and not vendor.uses_gh_cli(self.call.config):
                raise GitHubError(f"The GitHub App has no installation on {self.repo.split('/')[0]}.", 409)
            try:
                self._token = vendor.mint(connection_id=self.call.connection.id, creds=self.call.creds,
                                          config=self.call.config, installation_id=self.call.installation_id,
                                          repo=self.repo.split("/")[1], permissions=READ, now=self._clock(),
                                          transport=self.call.transport)
            except VendorError as exc:
                raise GitHubError(str(exc), exc.status) from exc
        return self._token

    @contextmanager
    def page(self) -> Iterator[None]:
        """Finish a page already fetched: calls inside go out despite a pause, unless GitHub itself refuses them."""
        self._in_page += 1
        try:
            yield
        finally:
            self._in_page -= 1

    def rename(self, repo: str) -> None:
        self.repo = repo
        self._token = None

    def paused_until(self) -> datetime | None:
        """The latest pause on this client's token, if one is still running."""
        found = [u for u in (paused_until(self._key(r), self._clock()) for r in (GRAPHQL, CORE)) if u is not None]
        return max(found, default=None)

    def _key(self, resource: str) -> tuple[Any, ...]:
        return self.call.connection.id, self.call.installation_id, resource

    def _pause(self, resource: str, until: datetime) -> None:
        key = self._key(resource)
        with _lock:
            if _paused.get(key) is None or _paused[key] < until:
                _paused[key] = until

    def _limited(self, response: httpx.Response) -> datetime | None:
        """When a 403 or 429 says to call again, or None when it isn't a rate limit."""
        now = self._clock()
        after = response.headers.get("Retry-After", "").strip()
        if after.isdigit():
            return now + timedelta(seconds=int(after))
        if response.headers.get("X-RateLimit-Remaining") == "0":
            return self._reset(response.headers.get("X-RateLimit-Reset"))
        try:
            message = str((response.json() or {}).get("message") or "")
        except (ValueError, AttributeError):
            message = ""
        return now + SECONDARY if "rate limit" in message.lower() else None

    def _reset(self, value: datetime | str | None) -> datetime:
        """A window's reset time, at least SECONDARY away, so a skewed clock never pauses into the past."""
        reset = _epoch(value) if isinstance(value, str) or value is None else value
        floor = self._clock() + SECONDARY
        return max(reset, floor) if reset is not None else floor

    def _send(self, resource: str, method: str, url: str, what: str, *,
              body: Callable[[], dict[str, Any]] | None = None, params: dict[str, Any] | None = None,
              accept: str | None = None) -> httpx.Response:
        for attempt in range(RETRIES + 1):
            until = None if self._in_page else paused_until(self._key(resource), self._clock())
            if until is not None:
                raise Paused(until)
            headers = vendor.headers(self.token())
            if accept:
                headers["Accept"] = accept
            try:
                response = vendor_call(self._http, method, url, what, headers=headers,
                                       json=body() if body else None, params=params)
            except VendorError as exc:
                raise GitHubError(str(exc), exc.status) from exc
            if response.status_code in RETRY and attempt < RETRIES:
                response.close()
                if resource == GRAPHQL:
                    # A heavy query times out as a 502, so every later page asks for half as much.
                    self.halvings += 1
                self._sleep(BACKOFF_S * 2 ** attempt)
                continue
            if response.status_code in LIMITED:
                until = self._limited(response)
                if until is not None:
                    self._pause(resource, until)
                    raise Paused(until)
            return response
        raise AssertionError("unreachable")

    def _answer(self, resource: str, data: Any, remaining: int | None, reset_at: datetime | None) -> Answer:
        found = Answer(data, remaining, self._reset(reset_at) if remaining is not None else reset_at)
        if found.pause_until is not None:
            self._pause(resource, found.pause_until)
        return found

    def graphql(self, query: str, variables: dict[str, Any]) -> Answer:
        """One query. Its ``first`` variable shrinks by half for each 502 or 503 this client has had."""
        def body() -> dict[str, Any]:
            sized = dict(variables)
            if _int(sized.get("first")):
                sized["first"] = max(1, sized["first"] >> self.halvings)
            return {"query": query, "variables": sized}

        what = "GitHub POST /graphql"
        response = self._send(GRAPHQL, "POST", self.graphql_url, what, body=body, accept=GRAPHQL_ACCEPT)
        if response.status_code >= 300:
            raise GitHubError(f"{what} answered HTTP {response.status_code}.", response.status_code)
        try:
            found = response.json()
        except ValueError as exc:
            raise GitHubError(f"{what} did not answer with JSON.") from exc
        errors = found.get("errors") if isinstance(found, dict) else None
        if errors:
            types = sorted({str(e.get("type")) for e in errors if isinstance(e, dict) and e.get("type")})
            if "RATE_LIMITED" in types:
                until = self._reset(response.headers.get("X-RateLimit-Reset"))
                self._pause(GRAPHQL, until)
                raise Paused(until)
            raise GraphQLError(f"GitHub GraphQL answered with errors: {', '.join(types) or 'untyped'}.", tuple(types))
        data = found.get("data") if isinstance(found, dict) else None
        if not isinstance(data, dict):
            raise GraphQLError("GitHub GraphQL answered with no data.")
        limit = data.get("rateLimit") if isinstance(data.get("rateLimit"), dict) else {}
        return self._answer(GRAPHQL, data, _int(limit.get("remaining")), _time(limit.get("resetAt")))

    def rest(self, path: str, params: dict[str, Any] | None = None) -> Answer:
        what = f"GitHub GET {path.split('?')[0]}"
        response = self._send(CORE, "GET", f"{self.base}{path}", what, params=params)
        if response.status_code >= 300:
            raise GitHubError(f"{what} answered HTTP {response.status_code}.", response.status_code)
        try:
            data = response.json()
        except ValueError as exc:
            raise GitHubError(f"{what} did not answer with JSON.") from exc
        remaining = response.headers.get("X-RateLimit-Remaining", "").strip()
        return self._answer(CORE, data, int(remaining) if remaining.isdigit() else None,
                            _epoch(response.headers.get("X-RateLimit-Reset")))
