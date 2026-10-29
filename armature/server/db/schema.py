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
    false,
)
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME
from sqlalchemy.dialects.mysql import MEDIUMTEXT, VARCHAR

from armature.events.summary import OUTCOMES as RUN_OUTCOMES

metadata = MetaData(naming_convention={
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
})

ROLES = ("admin", "team_lead", "project_lead", "contributor", "viewer")
PROVIDER_KINDS = ("bedrock", "anthropic", "claude-subscription", "openai-compatible", "ollama")
DOCUMENT_KINDS = ("role", "prompt", "mission", "skill")
WORKFLOW_KINDS = ("review", "fix", "merge", "port", "work", "other")
RUNNER_KEY_KINDS = ("standing", "ephemeral")
AWS_AUTH_KINDS = ("access_key", "assume_role", "access_key_assume_role")
AWS_STATES = ("unverified", "ok", "error")
ENVIRONMENT_KINDS = ("ec2-linux", "ec2-mac")
MARKETS = ("spot", "spot-then-on-demand", "on-demand")
HOST_STATES = ("allocating", "running", "releasing", "released")
ENVIRONMENT_PURPOSES = ("job", "warm")
ENVIRONMENT_STATES = ("requested", "booting", "ready", "running", "terminating", "terminated", "failed")
CONNECTION_KINDS = ("github_app", "slack", "jira", "linear")
CONNECTION_STATES = ("unverified", "ok", "error")
RISK_LEVELS = ("low", "medium", "high")
FINDING_DECISIONS = ("block", "suggest", "drop", "fixed")
OUTCOME_SOURCES = ("runner", "server")
GATE_STATES = ("open", "approved", "rejected", "expired", "canceled")
GATE_VIA = ("web", "slack")
CARD_STATES = ("post", "posted", "update", "done", "failed")
CONVERSATION_KINDS = ("slack", "web")
CONVERSATION_STATES = ("open", "closed")
CONVERSATION_AWAITING = ("user", "agent", "none")
MESSAGE_ROLES = ("user", "assistant", "system")
TRIGGER_POLL_STATUSES = ("ok", "error", "skipped")
TRIGGER_SEEN_OUTCOMES = ("queued", "seeded", "shadow", "skipped")

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
    Column("environment_type_id", String(36), ForeignKey("environment_types.id")),
    # connections.project_id points back here, so this side is added after both tables exist.
    Column("connection_id", String(36), ForeignKey("connections.id", use_alter=True)),
    Column("default_branch", String(191)),
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
    Column("slack_workspace_id", String(32)),
    Column("slack_user_id", String(32)),
    UniqueConstraint("oidc_iss", "oidc_sub"),
    UniqueConstraint("slack_workspace_id", "slack_user_id"),
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
    # Shipped with the server and stored from its file, so no one may edit it.
    Column("built_in", Boolean, nullable=False, default=False, server_default=false()),
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
    Column("project_id", String(36), ForeignKey("projects.id")),
    Column("scope", String(36), nullable=False, default=_scope_from_definition, index=True),
    Column("kind", String(32), nullable=False),
    Column("config", JSON),
    Column("aliases", JSON),
    _ts("created_at"),
    _ts("updated_at"),
    CheckConstraint(_in("kind", PROVIDER_KINDS), name="kind"),
    CheckConstraint(SCOPE_MATCHES_DEFINITION, name="scope"),
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
    Column("project_id", String(36), ForeignKey("projects.id")),
    Column("scope", String(36), nullable=False, default=_scope_from_definition, server_default=INSTANCE_SCOPE),
    Column("key", String(191), nullable=False),
    Column("value", JSON),
    _ts("updated_at", nullable=True),
    Column("updated_by", String(36)),
    PrimaryKeyConstraint("scope", "key"),
    CheckConstraint(SCOPE_MATCHES_DEFINITION, name="scope"),
)

WEBHOOK_SOURCES = ("github", "slack", "jira", "linear", "generic")
WEBHOOK_GENERIC_AUTH = ("hmac", "bearer")
WEBHOOK_APPROVALS = ("pending", "approved")
DELIVERY_STATUSES = ("accepted", "ignored", "duplicate", "rejected", "error", "held")

webhooks = Table(
    "webhooks", metadata,
    _id(),
    Column("team_id", String(36), ForeignKey("teams.id"), nullable=False, index=True),
    Column("name", name_type(191), nullable=False),
    Column("source", String(16), nullable=False),
    Column("path_token", String(64), nullable=False, unique=True),
    Column("secret_name", String(128)),
    Column("connection_id", String(36), ForeignKey("connections.id")),
    Column("generic_auth", String(16)),
    Column("generic_signature_header", String(64)),
    Column("generic_delivery_header", String(64)),
    Column("forward_url", Text),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("approval", String(16), nullable=False, server_default="pending"),
    Column("pinned_senders", JSON),
    _ts("created_at"),
    _ts("updated_at"),
    UniqueConstraint("team_id", "name"),
    CheckConstraint(_in("source", WEBHOOK_SOURCES), name="source"),
    CheckConstraint(f"generic_auth IS NULL OR {_in('generic_auth', WEBHOOK_GENERIC_AUTH)}", name="generic_auth"),
    CheckConstraint(_in("approval", WEBHOOK_APPROVALS), name="approval"),
    CheckConstraint("secret_name IS NOT NULL OR connection_id IS NOT NULL", name="secret"),
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
    Column("sender", String(255)),
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

# A poll or schedule trigger's runtime state. A claim moves next_run_at with a compare-and-set.
trigger_state = Table(
    "trigger_state", metadata,
    Column("trigger_id", String(36), ForeignKey("triggers.id"), primary_key=True),
    _ts("next_run_at", nullable=True),
    _ts("last_run_at", nullable=True),
    Column("last_status", String(16)),
    Column("last_error", Text),
    Column("last_matched", Integer),
    Column("last_new", Integer),
    Column("failures", Integer, nullable=False, default=0),
    _ts("seeded_at", nullable=True),
    Column("seeded_count", Integer),
    Index(None, "next_run_at"),
    CheckConstraint(_in("last_status", TRIGGER_POLL_STATUSES), name="last_status"),
)

# Every key a poll trigger has seen, so each is queued once ever. The primary key stops a second poller.
trigger_seen = Table(
    "trigger_seen", metadata,
    Column("trigger_id", String(36), ForeignKey("triggers.id"), nullable=False),
    Column("item_key", String(191), nullable=False),
    Column("outcome", String(16), nullable=False),
    # No foreign key: deleting a workflow deletes its jobs.
    Column("job_id", String(36)),
    _ts("first_seen_at"),
    _ts("last_seen_at"),
    PrimaryKeyConstraint("trigger_id", "item_key"),
    CheckConstraint(_in("outcome", TRIGGER_SEEN_OUTCOMES), name="outcome"),
)

trigger_polls = Table(
    "trigger_polls", metadata,
    _id(),
    Column("trigger_id", String(36), ForeignKey("triggers.id"), nullable=False),
    _ts("started_at"),
    _ts("finished_at", nullable=True),
    Column("status", String(16), nullable=False),
    Column("error", Text),
    Column("matched", Integer),
    Column("new_keys", JSON),
    Column("duration_ms", Integer),
    Index(None, "trigger_id", "started_at"),
    CheckConstraint(_in("status", TRIGGER_POLL_STATUSES), name="status"),
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
    Column("kind", String(16), nullable=False, default="workflow", server_default="workflow"),
    # The capabilities a runner must declare to claim the job. NULL, like an empty list, needs none.
    Column("requires", JSON),
    Column("runner_id", String(36), ForeignKey("runners.id"), index=True),
    _ts("cancel_requested_at", nullable=True),
    # The version each called workflow without a version: runs, fixed when the job was queued. NULL resolves at claim.
    Column("call_pins", JSON),
    # Set by the trigger and copied to every job that continues its work, so they all land in one thread.
    Column("thread", String(191)),
    # No foreign key: gates reference jobs, so this one would close a cycle.
    Column("resume_of_gate", String(36)),
    # Copied from the queuing rule's dry flag, so a later rule edit never makes a queued job live.
    Column("dry_run", Boolean, nullable=False, default=False, server_default=false()),
    # Jobs of one workflow that share a key run one at a time. NULL means no limit.
    Column("concurrency_key", String(191)),
    # A debounced job is not claimed before this.
    _ts("not_before", nullable=True),
    # No foreign key, as with delivery_id: the newer job of the same key that replaced this queued one.
    Column("superseded_by", String(36)),
    # No foreign key, as with superseded_by: the run this job reruns, copied to the run it opens.
    Column("rerun_of", String(36), index=True),
    Index(None, "status", "created_at"),
    # The backfill and a run's page find a run's jobs by run.
    Index(None, "run_id"),
    Index(None, "concurrency_key", "status"),
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
    Column("thread", String(191)),
    # Only the server writes risk and risk_reasons, from data it fetched itself. Never from a runner's events.
    Column("risk", String(8)),
    Column("risk_reasons", JSON),
    Column("outcome", String(16)),
    # Once the server writes an outcome, a runner's summary can't replace it.
    Column("outcome_source", String(8)),
    Column("head_sha", String(40)),
    # Where the run executed, as armature.events.location makes it. NULL until known or backfilled.
    Column("location", JSON),
    Index(None, "team_id", "started_at"),
    Index(None, "status"),
    Index(None, "subject", "started_at"),
    Index(None, "thread", "started_at"),
    Index(None, "project_id", "head_sha"),
    CheckConstraint(f"risk IS NULL OR {_in('risk', RISK_LEVELS)}", name="risk"),
    CheckConstraint(f"outcome IS NULL OR {_in('outcome', RUN_OUTCOMES)}", name="outcome"),
    CheckConstraint(f"outcome_source IS NULL OR {_in('outcome_source', OUTCOME_SOURCES)}", name="outcome_source"),
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
    Column("raised_by", JSON),
    Column("decision", String(16)),
    Column("reason", Text),
    Column("fix_sha", String(40)),
    Index(None, "project_id", "finding_key"),
    CheckConstraint(f"decision IS NULL OR {_in('decision', FINDING_DECISIONS)}", name="decision"),
)

# A head the server saw reviewed and approved. A push makes a new head with no mark.
review_marks = Table(
    "review_marks", metadata,
    _id(),
    Column("team_id", String(36), ForeignKey("teams.id"), nullable=False),
    Column("project_id", String(36), ForeignKey("projects.id"), nullable=False),
    Column("repo", String(191), nullable=False),
    Column("pr_number", Integer),
    Column("head_sha", String(40), nullable=False),
    Column("run_id", String(36), ForeignKey("runs.id"), nullable=False),
    Column("check_run_id", BigInteger),
    Column("app_id", Integer),
    Column("conclusion", String(16), nullable=False),
    _ts("created_at"),
    UniqueConstraint("project_id", "head_sha"),
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
    Column("kind", String(16), nullable=False, default="standing", server_default="standing"),
    _ts("expires_at", nullable=True),
    # The capabilities its runners may claim jobs for. NULL allows any.
    Column("capabilities", JSON),
    CheckConstraint(SCOPE_MATCHES_TEAM, name="scope"),
    CheckConstraint(_in("kind", RUNNER_KEY_KINDS), name="kind"),
)

# A host running `armature runner`, one row per key and name, kept when it stops so the Runners page can show it.
runners = Table(
    "runners", metadata,
    _id(),
    Column("runner_key_id", String(36), ForeignKey("runner_keys.id"), nullable=False),
    Column("name", name_type(191), nullable=False),
    Column("capabilities", JSON),
    Column("labels", JSON),
    Column("version", String(64)),
    Column("host", String(191)),
    Column("slots", Integer),
    Column("state", String(16), nullable=False, default="online"),
    _ts("created_at"),
    _ts("last_seen_at"),
    Column("platform", JSON),
    UniqueConstraint("runner_key_id", "name"),
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

# Instance-wide AWS settings. Never in ``secrets``, because those resolve into job bundles.
aws_connections = Table(
    "aws_connections", metadata,
    _id(),
    Column("name", name_type(191), nullable=False, unique=True),
    Column("region", String(32), nullable=False),
    Column("auth_kind", String(32), nullable=False),
    Column("sealed", Text),
    Column("key_id", String(8)),
    Column("vpc_id", String(64)),
    Column("subnets", JSON),
    Column("security_group_id", String(64)),
    Column("instance_profile", String(191)),
    # {"linux": "lt-...", "mac": "lt-..."}, from the stacks' LaunchTemplateIds and MacLaunchTemplateId outputs.
    Column("launch_template_ids", JSON),
    Column("mac_instance_profile", String(191)),
    Column("image_repository_uri", String(255)),
    # The host-role stack's AllowedInstanceTypes. A type outside it fails at launch with AccessDenied.
    Column("allowed_instance_types", JSON),
    Column("stack_name", String(128)),
    Column("session_role_arn", String(255)),
    Column("artifact_bucket", String(255)),
    Column("tag_project", String(64), nullable=False, default="armature-platform"),
    Column("extra_tags", JSON),
    Column("install_id", String(64), nullable=False),
    Column("allow_host_allocation", Boolean, nullable=False, default=False),
    Column("max_concurrent", Integer, nullable=False),
    Column("max_daily_launches", Integer, nullable=False),
    Column("daily_budget_usd", Numeric(10, 2)),
    Column("boot_timeout_s", Integer, nullable=False),
    Column("max_lifetime_s", Integer, nullable=False),
    Column("state", String(16), nullable=False, default="unverified"),
    Column("last_error", Text),
    _ts("verified_at", nullable=True),
    _ts("created_at"),
    _ts("updated_at"),
    Column("updated_by", String(36)),
    CheckConstraint(_in("auth_kind", AWS_AUTH_KINDS), name="auth_kind"),
    CheckConstraint(_in("state", AWS_STATES), name="state"),
    CheckConstraint("tag_project <> 'armature'", name="tag_project"),
)

environment_types = Table(
    "environment_types", metadata,
    _id(),
    Column("name", name_type(191), nullable=False, unique=True),
    Column("kind", String(16), nullable=False),
    # An AMI id for ec2-linux, a Tart image for ec2-mac.
    Column("image", String(191), nullable=False),
    Column("base_image", String(191)),
    Column("installs", JSON),
    Column("instance_types", JSON),
    Column("market", String(32), nullable=False, default="spot-then-on-demand"),
    Column("disk_gb", Integer),
    Column("vm_cpu", Integer),
    Column("vm_memory_gb", Integer),
    Column("capabilities", JSON),
    Column("boot_timeout_s", Integer),
    Column("job_timeout_s", Integer, nullable=False),
    Column("hourly_cost_usd", Numeric(10, 4)),
    Column("health_check", JSON),
    Column("enabled", Boolean, nullable=False, default=True),
    _ts("created_at"),
    _ts("updated_at"),
    Column("updated_by", String(36)),
    CheckConstraint(_in("kind", ENVIRONMENT_KINDS), name="kind"),
    CheckConstraint(_in("market", MARKETS), name="market"),
)

# An EC2 Mac dedicated host bills for at least 24 hours, so only an admin allocates or releases one.
environment_hosts = Table(
    "environment_hosts", metadata,
    _id(),
    Column("connection_id", String(36), ForeignKey("aws_connections.id"), nullable=False),
    Column("name", name_type(191), nullable=False, unique=True),
    Column("instance_id", String(64)),
    Column("dedicated_host_id", String(64)),
    Column("zone", String(32)),
    Column("instance_type", String(32)),
    Column("state", String(16), nullable=False),
    Column("slots", Integer, nullable=False, default=2),
    _ts("allocated_at"),
    _ts("min_release_at"),
    _ts("idle_since", nullable=True),
    Column("auto_release_after_s", Integer),
    Column("created_by", String(36)),
    CheckConstraint(_in("state", HOST_STATES), name="state"),
)

# slot_index goes NULL when an environment ends, so the unique key holds one live VM per host slot.
environments = Table(
    "environments", metadata,
    _id(),
    Column("connection_id", String(36), ForeignKey("aws_connections.id"), nullable=False),
    Column("type_id", String(36), ForeignKey("environment_types.id")),
    Column("purpose", String(8), nullable=False),
    Column("job_id", String(36), ForeignKey("jobs.id"), index=True),
    Column("runner_key_id", String(36), ForeignKey("runner_keys.id")),
    Column("team_id", String(36)),
    Column("project_id", String(36)),
    Column("host_id", String(36), ForeignKey("environment_hosts.id")),
    Column("slot_index", Integer),
    Column("provider_id", String(64)),
    Column("state", String(16), nullable=False, index=True),
    Column("market", String(32)),
    Column("instance_type", String(32)),
    Column("subnet_id", String(64)),
    Column("launch_attempts", Integer, nullable=False, default=0),
    Column("error", Text),
    Column("enroll_hmac", String(64), unique=True),
    _ts("enroll_expires_at", nullable=True),
    _ts("enrolled_at", nullable=True),
    _ts("requested_at"),
    _ts("launched_at", nullable=True),
    _ts("ready_at", nullable=True),
    _ts("terminate_after", nullable=True),
    _ts("terminated_at", nullable=True),
    UniqueConstraint("host_id", "slot_index"),
    CheckConstraint(_in("purpose", ENVIRONMENT_PURPOSES), name="purpose"),
    CheckConstraint(_in("state", ENVIRONMENT_STATES), name="state"),
)

# A vendor connection's credentials, sealed as one JSON object. Never in ``secrets``, which resolve into job bundles.
connections = Table(
    "connections", metadata,
    _id(),
    Column("team_id", String(36), ForeignKey("teams.id"), nullable=False, index=True),
    Column("project_id", String(36), ForeignKey("projects.id")),
    Column("scope", String(36), nullable=False, default=_scope_from_definition),
    Column("kind", String(16), nullable=False),
    Column("name", name_type(191), nullable=False),
    Column("config", JSON),
    Column("sealed", Text),
    Column("key_id", String(8)),
    Column("identity", JSON),
    Column("state", String(16), nullable=False, default="unverified"),
    Column("last_error", Text),
    _ts("verified_at", nullable=True),
    _ts("created_at"),
    _ts("updated_at"),
    Column("updated_by", String(36)),
    UniqueConstraint("scope", "name"),
    CheckConstraint(_in("kind", CONNECTION_KINDS), name="kind"),
    CheckConstraint(_in("state", CONNECTION_STATES), name="state"),
    CheckConstraint(SCOPE_MATCHES_DEFINITION, name="scope"),
)

# Filled by a GitHub App connection's Verify: which installation serves each repository owner.
github_installations = Table(
    "github_installations", metadata,
    _id(),
    Column("connection_id", String(36), ForeignKey("connections.id"), nullable=False),
    Column("owner", String(100), nullable=False),
    Column("installation_id", BigInteger, nullable=False),
    Column("permissions", JSON),
    UniqueConstraint("connection_id", "owner"),
)

TOOL_CALL_MODES = ("live", "dry")
TOOL_CALL_OUTCOMES = ("ok", "approved", "refused", "failed")

# One row per broker call a run's step made. Params are kept only for a dry call, redacted.
tool_calls = Table(
    "tool_calls", metadata,
    _id(),
    Column("run_id", String(36), ForeignKey("runs.id"), nullable=False, index=True),
    Column("team_id", String(36), nullable=False),
    Column("project_id", String(36)),
    Column("step_id", String(191), nullable=False),
    Column("tool", String(64), nullable=False),
    Column("mode", String(8), nullable=False),
    Column("outcome", String(16), nullable=False),
    Column("target", String(191)),
    Column("detail", JSON),
    _ts("created_at"),
    Index(None, "tool", "target", "created_at"),
    CheckConstraint(_in("mode", TOOL_CALL_MODES), name="mode"),
    CheckConstraint(_in("outcome", TOOL_CALL_OUTCOMES), name="outcome"),
)

# A human gate a run paused at. The checkpoint is the run's stage results, redacted, to resume from.
gates = Table(
    "gates", metadata,
    _id(),
    Column("run_id", String(36), ForeignKey("runs.id"), nullable=False, index=True),
    Column("job_id", String(36), ForeignKey("jobs.id")),
    Column("team_id", String(36), ForeignKey("teams.id"), nullable=False),
    Column("project_id", String(36), ForeignKey("projects.id")),
    Column("step_id", String(191), nullable=False),
    # The checkpoint key the answer goes under: the step id, or step__iter_N inside a loop.
    Column("step_key", String(191), nullable=False),
    Column("present", Text),
    Column("state", String(16), nullable=False, default="open"),
    Column("min_role", String(16), nullable=False, default="project_lead"),
    Column("approvers", JSON),
    Column("nonce", String(64), nullable=False),
    Column("checkpoint", LongText),
    Column("workflow_version", Integer),
    Column("thread", String(191)),
    Column("channel", JSON),
    _ts("created_at"),
    _ts("expires_at"),
    _ts("decided_at", nullable=True),
    Column("decided_by", String(36)),
    Column("decided_via", String(8)),
    Column("decision_note", Text),
    Column("resume_job_id", String(36), ForeignKey("jobs.id")),
    # The Slack card's outbox: what to do next, when it's due, the tries so far, and why it was given up.
    Column("card_state", String(8)),
    _ts("card_due_at", nullable=True),
    Column("card_attempts", Integer, nullable=False, default=0, server_default="0"),
    Column("card_error", Text),
    UniqueConstraint("run_id", "step_key"),
    Index(None, "state", "expires_at"),
    CheckConstraint(_in("state", GATE_STATES), name="state"),
    CheckConstraint(_in("min_role", ROLES), name="min_role"),
    CheckConstraint(_in("decided_via", GATE_VIA), name="decided_via"),
    CheckConstraint(f"card_state IS NULL OR {_in('card_state', CARD_STATES)}", name="card_state"),
)

# A Slack thread or web chat. Each user message queues one short turn, and active_job_id is the lock on that.
conversations = Table(
    "conversations", metadata,
    _id(),
    Column("team_id", String(36), ForeignKey("teams.id"), nullable=False),
    Column("project_id", String(36), ForeignKey("projects.id")),
    Column("workflow_id", String(36), ForeignKey("workflows.id"), nullable=False),
    Column("kind", String(8), nullable=False),
    Column("thread", String(191), nullable=False),
    Column("external", JSON),
    Column("owner_id", String(36), ForeignKey("users.id")),
    Column("state", String(8), nullable=False, default="open"),
    Column("awaiting", String(8), nullable=False, default="agent"),
    # Code-owned facts, such as the open proposal or the assistant's draft. A run changes them only through tools.
    Column("data", JSON),
    Column("active_job_id", String(36), ForeignKey("jobs.id"), index=True),
    Column("turns", Integer, nullable=False, default=0),
    _ts("created_at"),
    _ts("updated_at"),
    _ts("closed_at", nullable=True),
    UniqueConstraint("team_id", "thread"),
    CheckConstraint(_in("kind", CONVERSATION_KINDS), name="kind"),
    CheckConstraint(_in("state", CONVERSATION_STATES), name="state"),
    CheckConstraint(_in("awaiting", CONVERSATION_AWAITING), name="awaiting"),
)

# A NULL ext_ts never collides, so only Slack messages, which carry their ts, collapse on a retry.
conversation_messages = Table(
    "conversation_messages", metadata,
    _id(),
    Column("conversation_id", String(36), ForeignKey("conversations.id"), nullable=False, index=True),
    Column("seq", Integer, nullable=False),
    Column("role", String(16), nullable=False),
    Column("author", String(191)),
    Column("ext_ts", String(32)),
    Column("text", Text, nullable=False),
    Column("handled", Boolean, nullable=False, default=False),
    _ts("created_at"),
    # When the server's own message reached Slack. NULL on a Slack message still to send, or given up on.
    _ts("posted_at", nullable=True),
    # The Slack outbox: when the next try is due, the tries so far, and why it was given up.
    _ts("post_due_at", nullable=True),
    Column("post_attempts", Integer, nullable=False, default=0, server_default="0"),
    Column("post_error", Text),
    UniqueConstraint("conversation_id", "seq"),
    UniqueConstraint("conversation_id", "ext_ts"),
    CheckConstraint(_in("role", MESSAGE_ROLES), name="role"),
)

# A Slack account a user asked to link, waiting for the code the bot sent them in a direct message.
slack_links = Table(
    "slack_links", metadata,
    Column("user_id", String(36), ForeignKey("users.id"), primary_key=True),
    Column("workspace_id", String(32), nullable=False),
    Column("slack_user_id", String(32), nullable=False),
    Column("code_hmac", String(64), nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    _ts("created_at"),
    _ts("expires_at"),
)

# A Slack reply that came before its thread's first message did, held for that message's conversation a short while.
conversation_pending = Table(
    "conversation_pending", metadata,
    _id(),
    Column("team_id", String(36), ForeignKey("teams.id"), nullable=False),
    Column("thread", String(191), nullable=False),
    Column("ext_ts", String(32), nullable=False),
    Column("author", String(191)),
    Column("text", Text, nullable=False),
    # No foreign key: deliveries are pruned, and the drop is recorded on this one while it lasts.
    Column("delivery_id", String(36)),
    _ts("created_at"),
    UniqueConstraint("team_id", "thread", "ext_ts"),
    Index(None, "created_at"),
)
