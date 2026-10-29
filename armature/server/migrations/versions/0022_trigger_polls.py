"""Poll and schedule triggers, and per-key debounce and concurrency for jobs.

Revision ID: 0022
Revises: 0025
Create Date: 2026-11-03 09:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '0022'
down_revision: str | Sequence[str] | None = '0025'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The values armature.server.db.schema allowed at this revision, frozen here.
POLL_STATUSES = ('ok', 'error', 'skipped')
SEEN_OUTCOMES = ('queued', 'seeded', 'shadow', 'skipped')
POLL_KINDS = ('jira_poll', 'linear_poll', 'schedule')


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ts(name: str, nullable: bool) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=nullable)


# As in 0019: SQLite rebuilds jobs, and other tables reference it.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def upgrade() -> None:
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('concurrency_key', sa.String(length=191), nullable=True))
        batch.add_column(_ts('not_before', True))
        batch.add_column(sa.Column('superseded_by', sa.String(length=36), nullable=True))
    op.create_index(op.f('ix_jobs_concurrency_key_status'), 'jobs', ['concurrency_key', 'status'], unique=False)
    op.create_table('trigger_state',
    sa.Column('trigger_id', sa.String(length=36), nullable=False),
    _ts('next_run_at', True),
    _ts('last_run_at', True),
    sa.Column('last_status', sa.String(length=16), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('last_matched', sa.Integer(), nullable=True),
    sa.Column('last_new', sa.Integer(), nullable=True),
    sa.Column('failures', sa.Integer(), nullable=False),
    _ts('seeded_at', True),
    sa.Column('seeded_count', sa.Integer(), nullable=True),
    sa.CheckConstraint(_in('last_status', POLL_STATUSES), name=op.f('ck_trigger_state_last_status')),
    sa.ForeignKeyConstraint(['trigger_id'], ['triggers.id'], name=op.f('fk_trigger_state_trigger_id_triggers')),
    sa.PrimaryKeyConstraint('trigger_id', name=op.f('pk_trigger_state'))
    )
    op.create_index(op.f('ix_trigger_state_next_run_at'), 'trigger_state', ['next_run_at'], unique=False)
    op.create_table('trigger_seen',
    sa.Column('trigger_id', sa.String(length=36), nullable=False),
    sa.Column('item_key', sa.String(length=191), nullable=False),
    sa.Column('outcome', sa.String(length=16), nullable=False),
    sa.Column('job_id', sa.String(length=36), nullable=True),
    _ts('first_seen_at', False),
    _ts('last_seen_at', False),
    sa.CheckConstraint(_in('outcome', SEEN_OUTCOMES), name=op.f('ck_trigger_seen_outcome')),
    sa.ForeignKeyConstraint(['trigger_id'], ['triggers.id'], name=op.f('fk_trigger_seen_trigger_id_triggers')),
    sa.PrimaryKeyConstraint('trigger_id', 'item_key', name=op.f('pk_trigger_seen'))
    )
    op.create_table('trigger_polls',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('trigger_id', sa.String(length=36), nullable=False),
    _ts('started_at', False),
    _ts('finished_at', True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('matched', sa.Integer(), nullable=True),
    sa.Column('new_keys', sa.JSON(), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.CheckConstraint(_in('status', POLL_STATUSES), name=op.f('ck_trigger_polls_status')),
    sa.ForeignKeyConstraint(['trigger_id'], ['triggers.id'], name=op.f('fk_trigger_polls_trigger_id_triggers')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_trigger_polls'))
    )
    op.create_index(op.f('ix_trigger_polls_trigger_id_started_at'), 'trigger_polls', ['trigger_id', 'started_at'],
                    unique=False)
    _sqlite_foreign_keys(True)


def downgrade() -> None:
    """Drop poll state and the job columns. Poll triggers go, and superseded jobs read as canceled."""
    _sqlite_foreign_keys(False)
    op.drop_index(op.f('ix_trigger_polls_trigger_id_started_at'), table_name='trigger_polls')
    op.drop_table('trigger_polls')
    op.drop_table('trigger_seen')
    op.drop_index(op.f('ix_trigger_state_next_run_at'), table_name='trigger_state')
    op.drop_table('trigger_state')
    kinds = ', '.join(repr(k) for k in POLL_KINDS)
    op.execute(f"UPDATE jobs SET trigger_id = NULL WHERE trigger_id IN (SELECT id FROM triggers WHERE kind IN ({kinds}))")
    op.execute(f"DELETE FROM triggers WHERE kind IN ({kinds})")
    op.execute("UPDATE jobs SET status = 'canceled' WHERE status = 'superseded'")
    op.drop_index(op.f('ix_jobs_concurrency_key_status'), table_name='jobs')
    with op.batch_alter_table('jobs') as batch:
        batch.drop_column('superseded_by')
        batch.drop_column('not_before')
        batch.drop_column('concurrency_key')
    _sqlite_foreign_keys(True)
