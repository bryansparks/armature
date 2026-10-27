"""SQLAlchemy Core tables for the server (DESIGN.md, "Data model").

Portable types only, so the same metadata runs on SQLite, MySQL 8, and
Postgres. Ids are UUID strings. Indexed strings stay at 191 characters or
fewer, the longest utf8mb4 key MySQL accepts in a composite index.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME
from sqlalchemy.dialects.mysql import MEDIUMTEXT, VARCHAR

metadata = MetaData(naming_convention={
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
})

ROLES = ("admin", "team_lead", "project_lead", "contributor", "viewer")
PROVIDER_KINDS = ("bedrock", "anthropic", "claude-subscription", "openai-compatible", "ollama")
DOCUMENT_KINDS = ("role", "prompt", "mission")
WORKFLOW_KINDS = ("review", "fix", "merge", "port", "work", "other")

INSTANCE_SCOPE = "*"


class UTCDateTime(TypeDecorator):
    """Aware UTC datetimes in and out on every backend. SQLite and MySQL keep no offset, so UTC is implied."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "mysql":
            return dialect.type_descriptor(MYSQL_DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("naive datetime refused, pass an aware one")
        value = value.astimezone(UTC)
        return value if dialect.name == "postgresql" else value.replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


SCOPE_MATCHES_TEAM = f"(team_id IS NULL AND scope = '{INSTANCE_SCOPE}') OR (team_id IS NOT NULL AND scope = team_id)"


def scope_of(team_id: str | None) -> str:
    return INSTANCE_SCOPE if team_id is None else team_id


def _scope_from_team(context) -> str:
    return scope_of(context.get_current_parameters().get("team_id"))


# A project-scoped definition keeps its project's team_id too, so team visibility covers it.
SCOPE_MATCHES_DEFINITION = (f"(project_id IS NULL AND ({SCOPE_MATCHES_TEAM})) "
                            "OR (project_id IS NOT NULL AND team_id IS NOT NULL AND scope = project_id)")


def _scope_from_definition(context) -> str:
    params = context.get_current_parameters()
    return params.get("project_id") or scope_of(params.get("team_id"))


# MySQL TEXT stops at 64 KB, too small for a long prompt or workflow body.
LongText = Text().with_variant(MEDIUMTEXT(), "mysql")


# MySQL's default collation ignores case, so names that differ only in case would collide there and not elsewhere.
def name_type(length: int) -> String:
    return String(length).with_variant(VARCHAR(length, collation="utf8mb4_bin"), "mysql")


def _id() -> Column:
    return Column("id", String(36), primary_key=True)


def _ts(name: str, nullable: bool = False) -> Column:
    return Column(name, UTCDateTime(), nullable=nullable)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


teams = Table(
    "teams", metadata,
    _id(),
    Column("name", name_type(191), nullable=False, unique=True),
    Column("slug", String(40), unique=True),
    _ts("created_at"),
)

projects = Table(
    "projects", metadata,
    _id(),
    Column("team_id", String(36), ForeignKey("teams.id"), nullable=False),
    Column("name", name_type(191), nullable=False),
    Column("platform", String(32)),
    Column("enabled_workflows", JSON),
    Column("branch_filters", JSON),
    Column("repo", String(191)),
    _ts("created_at"),
    UniqueConstraint("team_id", "name"),
)

users = Table(
    "users", metadata,
    _id(),
    Column("username", String(64), unique=True),
    Column("display_name", String(191)),
    Column("email", String(191)),
    Column("oidc_iss", String(191)),
    Column("oidc_sub", String(191)),
    Column("password_hash", String(255)),
    Column("totp_secret_enc", Text),
    Column("last_totp_step", BigInteger, nullable=False, default=0),
    Column("is_admin", Boolean, nullable=False, default=False),
    Column("disabled", Boolean, nullable=False, default=False),
    _ts("totp_confirmed_at", nullable=True),
    _ts("created_at"),
    _ts("last_login_at", nullable=True),
    UniqueConstraint("oidc_iss", "oidc_sub"),
)

memberships = Table(
    "memberships", metadata,
    _id(),
    Column("user_id", String(36), ForeignKey("users.id"), nullable=False),
    Column("scope_kind", String(16), nullable=False),
    Column("scope_id", String(36), nullable=False),
    Column("role", String(16), nullable=False),
    Column("source", String(16), nullable=False, default="manual", server_default="manual"),
    _ts("created_at"),
    UniqueConstraint("user_id", "scope_kind", "scope_id"),
    CheckConstraint(_in("scope_kind", ("team", "project")), name="scope_kind"),
    CheckConstraint(_in("role", ROLES), name="role"),
    CheckConstraint(_in("source", ("manual", "oidc")), name="source"),
    Index(None, "scope_kind", "scope_id"),
)

invites = Table(
    "invites", metadata,
    _id(),
    Column("token_hmac", String(64), nullable=False, unique=True),
    Column("user_id", String(36), ForeignKey("users.id"), nullable=False, index=True),
    Column("created_by", String(36)),
    _ts("created_at"),
    _ts("expires_at"),
    _ts("used_at", nullable=True),
)

sessions = Table(
    "sessions", metadata,
    Column("token_hmac", String(64), primary_key=True),
    Column("user_id", String(36), ForeignKey("users.id"), nullable=False, index=True),
    Column("csrf", String(64), nullable=False),
    _ts("created_at"),
    _ts("last_seen_at"),
    _ts("step_up_at", nullable=True),
    Column("ip", String(45)),
)

login_attempts = Table(
    "login_attempts", metadata,
    _id(),
    Column("username", String(191)),
    Column("ip", String(45)),
    Column("succeeded", Boolean, nullable=False),
    _ts("at"),
    Index(None, "ip", "at"),
    Index(None, "username", "at"),
)

audit = Table(
    "audit", metadata,
    _id(),
    _ts("at"),
    Column("actor_id", String(36)),
    Column("team_id", String(36)),
    Column("action", String(64), nullable=False),
    Column("target_kind", String(32)),
    Column("target_id", String(191)),
    Column("detail", JSON),
    Column("ip", String(45)),
    Index(None, "team_id", "at"),
    Index(None, "at"),
)

workflows = Table(
    "workflows", metadata,
    _id(),
    Column("team_id", String(36), ForeignKey("teams.id"), index=True),
    Column("project_id", String(36), ForeignKey("projects.id")),
    Column("scope", String(36), nullable=False, default=_scope_from_definition),
    Column("name", name_type(191), nullable=False),
    Column("kind", String(16), nullable=False, default="other", server_default="other"),
    Column("current_version", Integer, nullable=False, default=0),
    _ts("created_at"),
    _ts("updated_at"),
    UniqueConstraint("scope", "name"),
    CheckConstraint(SCOPE_MATCHES_DEFINITION, name="scope"),
    CheckConstraint(_in("kind", WORKFLOW_KINDS), name="kind"),
)

documents = Table(
    "documents", metadata,
    _id(),
    Column("team_id", String(36), ForeignKey("teams.id")),
    Column("project_id", String(36), ForeignKey("projects.id")),
    Column("scope", String(36), nullable=False, default=_scope_from_definition),
    Column("kind", String(16), nullable=False),
    Column("key", name_type(191), nullable=False),
    Column("current_version", Integer, nullable=False, default=0),
    _ts("created_at"),
    _ts("updated_at"),
    CheckConstraint(_in("kind", DOCUMENT_KINDS), name="kind"),
    UniqueConstraint("scope", "kind", "key"),
    CheckConstraint(SCOPE_MATCHES_DEFINITION, name="scope"),
)

versions = Table(
    "versions", metadata,
    _id(),
    Column("object_kind", String(16), nullable=False),
    Column("object_id", String(36), nullable=False),
    Column("version", Integer, nullable=False),
    Column("body", LongText, nullable=False),
    Column("author_id", String(36)),
    _ts("created_at"),
    Column("note", Text),
    UniqueConstraint("object_kind", "object_id", "version"),
)

packs = Table(
    "packs", metadata,
    _id(),
    Column("name", String(191), nullable=False, unique=True),
    Column("version", String(64), nullable=False),
    Column("source", Text, nullable=False),
    Column("pinned_ref", String(191), nullable=False),
    Column("installed_by", String(36)),
    _ts("installed_at"),
)

providers = Table(
    "providers", metadata,
    _id(),
    Column("team_id", String(36), ForeignKey("teams.id"), nullable=False, index=True),
    Column("kind", String(32), nullable=False),
    Column("config", JSON),
    Column("aliases", JSON),
    _ts("created_at"),
    _ts("updated_at"),
    CheckConstraint(_in("kind", PROVIDER_KINDS), name="kind"),
)

secrets = Table(
    "secrets", metadata,
    _id(),
    Column("team_id", String(36), ForeignKey("teams.id")),
    Column("scope", String(36), nullable=False),
    Column("name", name_type(128), nullable=False),
    Column("ciphertext", Text, nullable=False),
    Column("key_id", String(8), nullable=False),
    _ts("updated_at"),
    Column("updated_by", String(36)),
    UniqueConstraint("scope", "name"),
    CheckConstraint(SCOPE_MATCHES_TEAM, name="scope"),
)

settings = Table(
    "settings", metadata,
    Column("team_id", String(36), ForeignKey("teams.id")),
    Column("scope", String(36), nullable=False, default=_scope_from_team, server_default=INSTANCE_SCOPE),
    Column("key", String(191), nullable=False),
    Column("value", JSON),
    _ts("updated_at", nullable=True),
    Column("updated_by", String(36)),
    PrimaryKeyConstraint("scope", "key"),
    CheckConstraint(SCOPE_MATCHES_TEAM, name="scope"),
)

WEBHOOK_SOURCES = ("github", "slack", "generic")
WEBHOOK_GENERIC_AUTH = ("hmac", "bearer")
DELIVERY_STATUSES = ("accepted", "ignored", "duplicate", "rejected", "error")

webhooks = Table(
    "webhooks", metadata,
    _id(),
    Column("team_id", String(36), ForeignKey("teams.id"), nullable=False, index=True),
    Column("name", name_type(191), nullable=False),
    Column("source", String(16), nullable=False),
    Column("path_token", String(64), nullable=False, unique=True),
    Column("secret_name", String(128), nullable=False),
    Column("generic_auth", String(16)),
    Column("generic_signature_header", String(64)),
    Column("generic_delivery_header", String(64)),
    Column("forward_url", Text),
    Column("enabled", Boolean, nullable=False, default=True),
    _ts("created_at"),
    _ts("updated_at"),
    UniqueConstraint("team_id", "name"),
    CheckConstraint(_in("source", WEBHOOK_SOURCES), name="source"),
    CheckConstraint(f"generic_auth IS NULL OR {_in('generic_auth', WEBHOOK_GENERIC_AUTH)}", name="generic_auth"),
)

# A NULL delivery_key never collides, so only keyed deliveries are deduplicated.
webhook_deliveries = Table(
    "webhook_deliveries", metadata,
    _id(),
    Column("webhook_id", String(36), ForeignKey("webhooks.id"), nullable=False),
    _ts("received_at"),
    Column("delivery_key", String(191)),
    Column("event", String(64)),
    Column("status", String(16), nullable=False),
    Column("reason", Text),
    Column("payload", LongText),
    Column("headers", JSON),
    Column("matched", JSON),
    Column("forward_status", String(32)),
    UniqueConstraint("webhook_id", "delivery_key"),
    Index(None, "webhook_id", "received_at"),
    Index(None, "received_at"),
    CheckConstraint(_in("status", DELIVERY_STATUSES), name="status"),
)

triggers = Table(
    "triggers", metadata,
    _id(),
    Column("team_id", String(36), ForeignKey("teams.id"), nullable=False),
    Column("workflow_id", String(36), ForeignKey("workflows.id"), nullable=False, index=True),
    Column("kind", String(32), nullable=False),
    Column("config", JSON),
    Column("enabled", Boolean, nullable=False, default=True),
    _ts("created_at"),
    Column("webhook_id", String(36), ForeignKey("webhooks.id"), index=True),
)

jobs = Table(
    "jobs", metadata,
    _id(),
    Column("team_id", String(36), ForeignKey("teams.id"), nullable=False),
    Column("project_id", String(36), ForeignKey("projects.id")),
    Column("workflow_id", String(36), ForeignKey("workflows.id"), nullable=False),
    Column("run_id", String(36), ForeignKey("runs.id")),
    Column("inputs", JSON),
    Column("status", String(16), nullable=False, default="queued"),
    Column("claimed_by", String(191)),
    _ts("claimed_at", nullable=True),
    _ts("heartbeat_at", nullable=True),
    _ts("lease_expires_at", nullable=True),
    Column("attempts", Integer, nullable=False, default=0),
    _ts("created_at"),
    Column("trigger_id", String(36), ForeignKey("triggers.id")),
    # No foreign key: deliveries are pruned after two weeks and their jobs stay.
    Column("delivery_id", String(36)),
    Column("workflow_version", Integer),
    Column("error", Text),
    _ts("finished_at", nullable=True),
    Index(None, "status", "created_at"),
)

runs = Table(
    "runs", metadata,
    _id(),
    Column("team_id", String(36), ForeignKey("teams.id"), nullable=False),
    Column("project_id", String(36), ForeignKey("projects.id")),
    Column("workflow_id", String(36), ForeignKey("workflows.id")),
    Column("workflow_version", Integer),
    Column("status", String(16), nullable=False),
    Column("triggered_by", String(36)),
    Column("trigger_kind", String(32)),
    Column("cancelled_by", String(36)),
    Column("rerun_of", String(36)),
    _ts("started_at"),
    _ts("finished_at", nullable=True),
    Column("cost_usd", Numeric(14, 6)),
    Column("input_tokens", BigInteger),
    Column("output_tokens", BigInteger),
    Column("summary", JSON),
    Column("workflow_name", String(191)),
    Column("actor", String(191)),
    Column("trigger_ref", JSON),
    Column("runner_key_id", String(36)),
    Column("error", Text),
    _ts("last_event_at", nullable=True),
    Column("subject", String(191)),
    Index(None, "team_id", "started_at"),
    Index(None, "status"),
    Index(None, "subject", "started_at"),
)

# step_key is parent_step/step_id for a subagent's or nested run's step, so it never merges into the parent's row.
run_steps = Table(
    "run_steps", metadata,
    Column("run_id", String(36), ForeignKey("runs.id"), nullable=False),
    Column("step_key", String(191), nullable=False),
    Column("step_id", String(191), nullable=False),
    Column("status", String(16), nullable=False),
    _ts("started_at", nullable=True),
    _ts("finished_at", nullable=True),
    Column("cost_usd", Numeric(14, 6)),
    Column("input_tokens", BigInteger),
    Column("output_tokens", BigInteger),
    Column("error", Text),
    Column("output", JSON),
    Column("parent_step", String(191)),
    PrimaryKeyConstraint("run_id", "step_key"),
)

# Only the primary key: the event writer does one batched insert a second.
run_events = Table(
    "run_events", metadata,
    Column("run_id", String(36), nullable=False, autoincrement=False),
    Column("seq", BigInteger, nullable=False, autoincrement=False),
    _ts("ts"),
    Column("kind", String(32), nullable=False),
    Column("step_id", String(191)),
    Column("payload", JSON),
    PrimaryKeyConstraint("run_id", "seq"),
)

findings = Table(
    "findings", metadata,
    _id(),
    Column("run_id", String(36), ForeignKey("runs.id"), nullable=False, index=True),
    Column("project_id", String(36), ForeignKey("projects.id")),
    Column("finding_key", String(64), nullable=False),
    Column("path", Text),
    Column("line", Integer),
    Column("severity", String(16)),
    Column("disposition", String(32)),
    Column("body", Text),
    Column("title", Text),
    Column("step_id", String(191)),
    _ts("created_at", nullable=True),
    Index(None, "project_id", "finding_key"),
)

# A later artifact with the same name replaces the earlier one.
run_artifacts = Table(
    "run_artifacts", metadata,
    Column("run_id", String(36), ForeignKey("runs.id"), nullable=False),
    Column("name", name_type(100), nullable=False),
    Column("step_id", String(191)),
    Column("media_type", String(64), nullable=False),
    Column("content", LongText, nullable=False),
    Column("size", Integer, nullable=False),
    _ts("created_at"),
    PrimaryKeyConstraint("run_id", "name"),
)

runner_keys = Table(
    "runner_keys", metadata,
    _id(),
    Column("team_id", String(36), ForeignKey("teams.id"), index=True),
    Column("scope", String(36), nullable=False, default=_scope_from_team),
    Column("name", name_type(191), nullable=False),
    Column("key_hmac", String(64), nullable=False, unique=True),
    Column("created_by", String(36)),
    _ts("created_at"),
    _ts("last_used_at", nullable=True),
    _ts("revoked_at", nullable=True),
    CheckConstraint(SCOPE_MATCHES_TEAM, name="scope"),
)

run_tokens = Table(
    "run_tokens", metadata,
    Column("token_hmac", String(64), primary_key=True),
    Column("run_id", String(36), ForeignKey("runs.id"), nullable=False, index=True),
    Column("team_id", String(36), nullable=False),
    _ts("expires_at"),
)

OPERATION_OUTCOMES = ("ok", "refused", "failed")

# active_run and active_action hold the run and action while the command runs, so their unique keys allow one at a time.
operations = Table(
    "operations", metadata,
    _id(),
    Column("action_id", String(64), nullable=False),
    Column("run_id", String(36), ForeignKey("runs.id"), nullable=False),
    Column("requested_by", String(36)),
    _ts("started_at"),
    _ts("finished_at", nullable=True),
    Column("exit_code", Integer),
    Column("outcome", String(16)),
    Column("message", Text),
    Column("log", LongText),
    Column("active_run", String(36), unique=True),
    Column("active_action", String(64), unique=True),
    Index(None, "run_id", "started_at"),
    Index(None, "started_at"),
    CheckConstraint(f"outcome IS NULL OR {_in('outcome', OPERATION_OUTCOMES)}", name="outcome"),
)
