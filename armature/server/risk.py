"""How risky a change is: path floors the model can raise but never lower, and the run's recorded level.

Floors are code defaults plus the ``risk.floors`` setting, which can only add patterns. Missing or unknown
risk reads as high wherever it gates anything.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from fnmatch import fnmatchcase
from typing import Any, NamedTuple

from sqlalchemy import Connection, or_, select, update

from armature.server.db.schema import INSTANCE_SCOPE, RISK_LEVELS, runs, settings

LEVELS = RISK_LEVELS
FLOORS_KEY = "risk.floors"
PANEL_KEY = "review.low_risk_panel"
SETTINGS = (FLOORS_KEY, PANEL_KEY)
DEFAULT_LOW_RISK_PANEL = ("correctness", "security", "conventions", "simplicity")
SPECIALISTS = ("simplicity", "correctness", "security", "performance", "beauty", "test_adequacy", "concurrency",
               "conventions", "compatibility")
# The pull request files API stops at 3000, so a longer diff is one the server can't fully read.
FILES_MAX = 3000
MAX_REASONS = 20
PATTERNS_MAX = 200
PATTERN_MAX = 200
ROLE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
PANEL_MAX = 20

class Floor(NamedTuple):
    level: str
    label: str
    globs: tuple[str, ...] = ()
    # Whole words of a directory or file stem, so auth/ and AuthStore.swift count and author.md never does.
    words: frozenset[str] = frozenset()


# A glob without a slash matches any one directory or file name. With one, it matches the whole path, and a leading
# **/ also matches at the root. Matching ignores case, so a floor can't be dodged by renaming. New network endpoints
# and clients count only for added files, so NEW_NETWORK is checked against those alone.
DEFAULT_FLOORS: tuple[Floor, ...] = (
    Floor("high", "migrations and schema", (
        "**/migrations/**", "**/migration/**", "**/migrate/**", "**/alembic/**", "*.sql", "schema.*", "*.prisma",
        "**/schema/**")),
    Floor("high", "auth, sessions, and crypto", ("*.pem",), frozenset({
        "auth", "oauth", "authn", "authz", "authentication", "authorization", "login", "session", "sessions",
        "crypto", "cryptography", "keychain", "keystore", "password", "passwords", "credential", "credentials"})),
    Floor("high", "payments", ("*payment*", "*billing*", "*checkout*", "*invoice*")),
    Floor("high", "infrastructure and CI", (
        "deploy/**", "**/deploy/**", "infra/**", "**/infra/**", "**/terraform/**", "*.tf", "**/k8s/**",
        "**/helm/**", "Dockerfile*", "*.dockerfile", "docker-compose*", ".github/workflows/**", ".gitlab-ci.yml",
        ".circleci/**", "Jenkinsfile", "buildspec.yml", "*cloudformation*")),
    Floor("high", "permission manifests", ("AndroidManifest.xml", "*.entitlements", "Info.plist", "*.xcprivacy")),
    Floor("medium", "dependencies", (
        "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "requirements*.txt", "pyproject.toml",
        "poetry.lock", "uv.lock", "Pipfile*", "Gemfile*", "go.mod", "go.sum", "Cargo.toml", "Cargo.lock",
        "Podfile*", "Package.swift", "Package.resolved", "*.gradle", "*.gradle.kts", "libs.versions.toml",
        "gradle.properties", "pom.xml", "*.csproj", "packages.lock.json", "composer.json", "composer.lock")),
    Floor("medium", "build configuration", (
        "Makefile", "*.pbxproj", "*.xcconfig", "*.xcscheme", "tsconfig*.json", "vite.config.*", "webpack.config.*",
        "babel.config.*", "setup.py", "setup.cfg", "CMakeLists.txt", "*.cmake", "proguard-rules.pro")),
)
NEW_NETWORK = Floor("high", "new network endpoint or client", ("*.proto", "*.graphql", "openapi*", "swagger*"),
                    frozenset({"api", "apis", "client", "clients", "endpoint", "endpoints", "route", "routes",
                               "router", "controller", "controllers", "webhook", "webhooks", "grpc", "graphql",
                               "http", "rest", "network", "networking"}))
WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


class Invalid(ValueError):
    """A risk or review setting that fails its check. The message is for the person."""


def rank(level: Any) -> int:
    """Where ``level`` sits in LEVELS. Anything else counts as high."""
    return LEVELS.index(level) if level in LEVELS else len(LEVELS) - 1


def known(level: Any) -> str:
    return LEVELS[rank(level)]


def _glob(path: str, pattern: str) -> bool:
    path, pattern = path.lower(), pattern.lower()
    if "/" not in pattern:
        return any(fnmatchcase(part, pattern) for part in path.split("/"))
    return fnmatchcase(path, pattern) or (pattern.startswith("**/") and fnmatchcase(path, pattern[3:]))


def words(path: str) -> set[str]:
    """The lowercase words of each directory and of the file's stem: OAuthHelper.kt gives o, auth, and helper."""
    *dirs, name = path.split("/")
    return {word.lower() for part in (*dirs, name.split(".", 1)[0]) for word in WORD.findall(part)}


def _hits(path: str, rule: Floor) -> bool:
    return any(_glob(path, pattern) for pattern in rule.globs) or bool(rule.words & words(path))


def _rules(extra: dict[str, list[str]] | None) -> list[Floor]:
    rules = list(DEFAULT_FLOORS)
    for level in ("high", "medium"):
        patterns = tuple((extra or {}).get(level) or ())
        if patterns:
            rules.append(Floor(level, "added floor", patterns))
    return sorted(rules, key=lambda rule: -rank(rule.level))


def floor(paths: Iterable[str], extra: dict[str, list[str]] | None = None, total: int | None = None,
          added: Iterable[str] = ()) -> tuple[str, list[dict[str, str]]]:
    """The lowest level ``paths`` allow, and why. ``total`` is the diff's file count when the list was cut short, and
    ``added`` the paths the diff creates."""
    paths = [p for p in paths if isinstance(p, str) and p]
    count = max(len(paths), total or 0)
    if count > FILES_MAX:
        return "high", [{"source": "floor", "text": f"{count} files changed, more than the {FILES_MAX} "
                                                    "the server can read"}]
    level, reasons = "low", []
    for rule in _rules(extra):
        hit = next((p for p in paths if _hits(p, rule)), None)
        if hit is None:
            continue
        level = max(level, rule.level, key=rank)
        reasons.append({"source": "floor", "text": f"{rule.label}: {hit}"})
    hit = next((p for p in added if isinstance(p, str) and _hits(p, NEW_NETWORK)), None)
    if hit is not None:
        level = "high"
        reasons.append({"source": "floor", "text": f"{NEW_NETWORK.label}: {hit}"})
    return level, reasons[:MAX_REASONS]


def combine(floor_level: Any, model_level: Any) -> str:
    """The higher of the two. A model level the server doesn't know counts as high."""
    return max(known(floor_level), known(model_level), key=rank)


def record(conn: Connection, run_id: str, level: Any, reasons: list[dict[str, str]]) -> str:
    """Set the run's risk to ``level`` unless it already holds a higher one. Returns the level it holds after."""
    level = known(level)
    current = conn.execute(select(runs.c.risk, runs.c.risk_reasons).where(runs.c.id == run_id)).first()
    if current is None:
        raise LookupError(run_id)
    kept = list(current.risk_reasons or []) if current.risk == level else []
    merged = kept + [r for r in reasons if r not in kept]
    # The WHERE, not the read above, keeps a concurrent higher write from being lowered.
    conn.execute(update(runs).where(runs.c.id == run_id, or_(runs.c.risk.is_(None),
                                                             runs.c.risk.in_(LEVELS[:rank(level) + 1])))
                 .values(risk=level, risk_reasons=merged[:MAX_REASONS]))
    return conn.execute(select(runs.c.risk).where(runs.c.id == run_id)).scalar()


def panel(level: Any, low_risk: list[str]) -> list[str]:
    """The specialists a review at ``level`` runs: the low-risk panel when low, else every one."""
    return list(low_risk) if known(level) == "low" else list(SPECIALISTS)


def recorded(conn: Connection, run_id: str | None) -> str:
    """The run's risk for a gate. A missing run or a run with no risk reads as high."""
    if run_id is None:
        return "high"
    return known(conn.execute(select(runs.c.risk).where(runs.c.id == run_id)).scalar())


def _patterns(value: Any, level: str) -> list[str]:
    items = value.get(level, []) if isinstance(value, dict) else None
    if not isinstance(items, list):
        raise Invalid(f"{FLOORS_KEY}.{level} must be a list of path patterns.")
    cleaned: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip() or len(item) > PATTERN_MAX or not item.isprintable():
            raise Invalid(f"A path pattern is 1 to {PATTERN_MAX} printable characters.")
        if item.strip() not in cleaned:
            cleaned.append(item.strip())
    if len(cleaned) > PATTERNS_MAX:
        raise Invalid(f"{FLOORS_KEY}.{level} holds at most {PATTERNS_MAX} patterns.")
    return cleaned


def check_setting(key: str, value: Any) -> Any:
    """The value to store for ``risk.floors`` or ``review.low_risk_panel``. Raises Invalid for the person."""
    if key == FLOORS_KEY:
        if not isinstance(value, dict) or set(value) - {"high", "medium"}:
            raise Invalid(f"{FLOORS_KEY} is an object with high and medium lists of path patterns. "
                          "They add to the built-in floors and can't lower them.")
        return {level: _patterns(value, level) for level in ("high", "medium") if level in value}
    if not isinstance(value, list) or not value or len(value) > PANEL_MAX:
        raise Invalid(f"{PANEL_KEY} is a list of 1 to {PANEL_MAX} specialist role names.")
    roles: list[str] = []
    for role in value:
        if not isinstance(role, str) or not ROLE.fullmatch(role):
            raise Invalid("A role name is lowercase letters, digits, '_', or '-'.")
        if role not in roles:
            roles.append(role)
    return roles


def _scopes(team_id: str | None, project_id: str | None) -> list[str]:
    return [s for s in (project_id, team_id, INSTANCE_SCOPE) if s]


def _stored(conn: Connection, key: str, team_id: str | None, project_id: str | None) -> list[tuple[str, Any]]:
    order = _scopes(team_id, project_id)
    rows = {row.scope: row.value for row in conn.execute(
        select(settings.c.scope, settings.c.value).where(settings.c.scope.in_(order), settings.c.key == key))}
    found = []
    for scope in order:
        if scope not in rows:
            continue
        try:
            found.append((scope, check_setting(key, rows[scope])))
        except Invalid:
            continue
    return found


def extra_floors(conn: Connection, team_id: str | None, project_id: str | None) -> dict[str, list[str]]:
    """Every scope's added patterns together, since a floor only ever adds."""
    merged: dict[str, list[str]] = {"high": [], "medium": []}
    for _, value in _stored(conn, FLOORS_KEY, team_id, project_id):
        for level, patterns in value.items():
            merged[level] += [p for p in patterns if p not in merged[level]]
    return merged


def low_risk_panel(conn: Connection, team_id: str | None, project_id: str | None) -> list[str]:
    """The specialists a low-risk review runs: the narrowest scope's setting, else the default four."""
    found = _stored(conn, PANEL_KEY, team_id, project_id)
    return found[0][1] if found else list(DEFAULT_LOW_RISK_PANEL)
