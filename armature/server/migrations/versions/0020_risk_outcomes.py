"""Risk and outcomes: a run's server-decided risk and typed outcome, the lead's decision on each finding, review
marks, and the default risk-rubric skill.

Revision ID: 0020
Revises: 0017
Create Date: 2026-10-28 18:00:00

"""
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '0020'
down_revision: str | Sequence[str] | None = '0017'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The values armature.server.db.schema allowed at this revision, frozen here.
RISK_LEVELS = ('low', 'medium', 'high')
OUTCOMES = ('pr_opened', 'pushed', 'recommended', 'approved', 'blocked', 'noop')
DECISIONS = ('block', 'suggest', 'drop', 'fixed')
OUTCOME_SOURCES = ('runner', 'server')

RUBRIC_KEY = 'risk-rubric'
RUBRIC = """---
name: risk-rubric
description: Rate how risky a code change is to merge, as low, medium, or high, with a reason for each signal.
---

# Risk rubric

Read the diff and rate the change. Reply with JSON: {"level": "low" | "medium" | "high", "reasons": ["..."]}.
Give one short reason per signal you saw. The server raises your level to its own floors, so never argue a
change down to fit them.

## High

- Database migrations or schema changes.
- Authentication, sessions, tokens, permissions, or cryptography.
- Payments or billing.
- Infrastructure, deployment, or CI configuration.
- Permission manifests: AndroidManifest.xml, entitlements, Info.plist privacy keys.
- A new network endpoint, or a new client for an outside service.
- Deleting or weakening tests that guard any of the above.

## Medium

- Dependency manifests and lockfiles.
- Build configuration.
- Behavior changes across several modules, or public API changes.
- Concurrency, caching, or retry logic.

## Low

- Copy, comments, docs, styling, and tests alone.
- A small change inside one module with tests that cover it.

When unsure between two levels, pick the higher one.
"""

documents = sa.table('documents', sa.column('id', sa.String), sa.column('team_id', sa.String),
                     sa.column('project_id', sa.String), sa.column('scope', sa.String), sa.column('kind', sa.String),
                     sa.column('key', sa.String), sa.column('current_version', sa.Integer),
                     sa.column('created_at', sa.DateTime(timezone=True)),
                     sa.column('updated_at', sa.DateTime(timezone=True)))
versions = sa.table('versions', sa.column('id', sa.String), sa.column('object_kind', sa.String),
                    sa.column('object_id', sa.String), sa.column('version', sa.Integer), sa.column('body', sa.Text),
                    sa.column('created_at', sa.DateTime(timezone=True)), sa.column('note', sa.Text))


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ts(name: str, nullable: bool) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=nullable)


# As in 0015: SQLite rebuilds runs and findings, and other tables reference them.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def _seed_rubric() -> None:
    conn = op.get_bind()
    found = conn.execute(sa.select(documents.c.id).where(documents.c.scope == '*', documents.c.kind == 'skill',
                                                         documents.c.key == RUBRIC_KEY)).first()
    if found is not None:
        return
    # schema.UTCDateTime stores naive UTC everywhere but Postgres.
    now = datetime.now(UTC)
    now = now if conn.dialect.name == "postgresql" else now.replace(tzinfo=None)
    doc_id = str(uuid.uuid4())
    conn.execute(sa.insert(documents).values(id=doc_id, team_id=None, project_id=None, scope='*', kind='skill',
                                             key=RUBRIC_KEY, current_version=1, created_at=now, updated_at=now))
    conn.execute(sa.insert(versions).values(id=str(uuid.uuid4()), object_kind='document', object_id=doc_id,
                                            version=1, body=RUBRIC, created_at=now, note='Default risk rubric.'))


def upgrade() -> None:
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('runs') as batch:
        batch.add_column(sa.Column('risk', sa.String(length=8), nullable=True))
        batch.add_column(sa.Column('risk_reasons', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('outcome', sa.String(length=16), nullable=True))
        batch.add_column(sa.Column('outcome_source', sa.String(length=8), nullable=True))
        batch.add_column(sa.Column('head_sha', sa.String(length=40), nullable=True))
        batch.create_index(op.f('ix_runs_project_id_head_sha'), ['project_id', 'head_sha'], unique=False)
        batch.create_check_constraint(op.f('ck_runs_risk'), f"risk IS NULL OR {_in('risk', RISK_LEVELS)}")
        batch.create_check_constraint(op.f('ck_runs_outcome'), f"outcome IS NULL OR {_in('outcome', OUTCOMES)}")
        batch.create_check_constraint(op.f('ck_runs_outcome_source'),
                                      f"outcome_source IS NULL OR {_in('outcome_source', OUTCOME_SOURCES)}")
    with op.batch_alter_table('findings') as batch:
        batch.add_column(sa.Column('raised_by', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('decision', sa.String(length=16), nullable=True))
        batch.add_column(sa.Column('reason', sa.Text(), nullable=True))
        batch.add_column(sa.Column('fix_sha', sa.String(length=40), nullable=True))
        batch.create_check_constraint(op.f('ck_findings_decision'),
                                      f"decision IS NULL OR {_in('decision', DECISIONS)}")
    op.create_table('review_marks',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('repo', sa.String(length=191), nullable=False),
    sa.Column('pr_number', sa.Integer(), nullable=True),
    sa.Column('head_sha', sa.String(length=40), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('check_run_id', sa.BigInteger(), nullable=True),
    sa.Column('app_id', sa.Integer(), nullable=True),
    sa.Column('conclusion', sa.String(length=16), nullable=False),
    _ts('created_at', False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_review_marks_project_id_projects')),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_review_marks_run_id_runs')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_review_marks_team_id_teams')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_review_marks')),
    sa.UniqueConstraint('project_id', 'head_sha', name=op.f('uq_review_marks_project_id_head_sha'))
    )
    _sqlite_foreign_keys(True)
    _seed_rubric()


def downgrade() -> None:
    """Drop marks, risk, outcomes, and decisions. The rubric stays, since 0017 already allows skill documents."""
    _sqlite_foreign_keys(False)
    op.drop_table('review_marks')
    with op.batch_alter_table('findings') as batch:
        batch.drop_constraint(op.f('ck_findings_decision'), type_='check')
        batch.drop_column('fix_sha')
        batch.drop_column('reason')
        batch.drop_column('decision')
        batch.drop_column('raised_by')
    with op.batch_alter_table('runs') as batch:
        batch.drop_constraint(op.f('ck_runs_outcome_source'), type_='check')
        batch.drop_constraint(op.f('ck_runs_outcome'), type_='check')
        batch.drop_constraint(op.f('ck_runs_risk'), type_='check')
        batch.drop_index(op.f('ix_runs_project_id_head_sha'))
        batch.drop_column('head_sha')
        batch.drop_column('outcome_source')
        batch.drop_column('outcome')
        batch.drop_column('risk_reasons')
        batch.drop_column('risk')
    _sqlite_foreign_keys(True)
