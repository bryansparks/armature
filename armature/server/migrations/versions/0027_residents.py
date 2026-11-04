"""Residents: one long-lived instance of a workflow, its event queue, and the job that runs each turn.

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-03 09:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '0027'
down_revision: str | Sequence[str] | None = '0026'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The values armature.server.db.schema allowed at this revision, frozen here.
STATES = ('idle', 'paused', 'closed')
KINDS = ('request', 'ask', 'schedule', 'signal', 'idle', 'custom')


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ts(name: str, nullable: bool) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=nullable)


# As in 0019: SQLite rebuilds jobs, and gates reference it.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def upgrade() -> None:
    _sqlite_foreign_keys(False)
    op.create_table('residents',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=True),
    sa.Column('workflow_id', sa.String(length=36), nullable=False),
    sa.Column('workflow_version', sa.Integer(), nullable=False),
    sa.Column('instance_key', sa.String(length=191), nullable=False),
    sa.Column('state', sa.String(length=8), nullable=False),
    sa.Column('data', sa.JSON(), nullable=True),
    sa.Column('data_version', sa.Integer(), nullable=False),
    sa.Column('staged', sa.JSON(), nullable=True),
    sa.Column('active_job_id', sa.String(length=36), nullable=True),
    sa.Column('turns', sa.Integer(), nullable=False),
    sa.Column('turns_day', sa.Integer(), nullable=False),
    sa.Column('idle_turns_day', sa.Integer(), nullable=False),
    sa.Column('emits_day', sa.Integer(), nullable=False),
    sa.Column('idle_streak', sa.Integer(), nullable=False),
    _ts('next_idle_at', True),
    _ts('last_event_at', True),
    _ts('last_turn_at', True),
    sa.Column('spent_usd_day', sa.BigInteger(), nullable=False),
    sa.Column('spent_day', sa.String(length=10), nullable=True),
    _ts('created_at', False),
    _ts('updated_at', False),
    _ts('closed_at', True),
    sa.Column('close_reason', sa.String(length=32), nullable=True),
    sa.CheckConstraint(_in('state', STATES), name=op.f('ck_residents_state')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_residents_team_id_teams')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_residents_project_id_projects')),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], name=op.f('fk_residents_workflow_id_workflows')),
    sa.ForeignKeyConstraint(['active_job_id'], ['jobs.id'], name=op.f('fk_residents_active_job_id_jobs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_residents')),
    sa.UniqueConstraint('workflow_id', 'instance_key', name=op.f('uq_residents_workflow_id_instance_key'))
    )
    op.create_index(op.f('ix_residents_active_job_id'), 'residents', ['active_job_id'], unique=False)
    op.create_index(op.f('ix_residents_state_next_idle_at'), 'residents', ['state', 'next_idle_at'], unique=False)
    op.create_table('resident_events',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('resident_id', sa.String(length=36), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=True),
    sa.Column('source', sa.JSON(), nullable=True),
    sa.Column('reply_to', sa.JSON(), nullable=True),
    sa.Column('reply', sa.JSON(), nullable=True),
    sa.Column('dedupe_key', sa.String(length=191), nullable=True),
    sa.Column('dry_run', sa.Boolean(), nullable=False),
    sa.Column('handled', sa.Boolean(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('handled_by_job', sa.String(length=36), nullable=True),
    _ts('created_at', False),
    sa.CheckConstraint(_in('kind', KINDS), name=op.f('ck_resident_events_kind')),
    sa.ForeignKeyConstraint(['resident_id'], ['residents.id'], name=op.f('fk_resident_events_resident_id_residents')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_resident_events')),
    sa.UniqueConstraint('resident_id', 'seq', name=op.f('uq_resident_events_resident_id_seq')),
    sa.UniqueConstraint('resident_id', 'dedupe_key', name=op.f('uq_resident_events_resident_id_dedupe_key'))
    )
    op.create_index(op.f('ix_resident_events_resident_id'), 'resident_events', ['resident_id'], unique=False)
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('resident_id', sa.String(length=36), nullable=True))
        batch.create_index(batch.f('ix_jobs_resident_id'), ['resident_id'])
    _sqlite_foreign_keys(True)


def downgrade() -> None:
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('jobs') as batch:
        batch.drop_index(batch.f('ix_jobs_resident_id'))
        batch.drop_column('resident_id')
    op.drop_index(op.f('ix_resident_events_resident_id'), table_name='resident_events')
    op.drop_table('resident_events')
    op.drop_index(op.f('ix_residents_state_next_idle_at'), table_name='residents')
    op.drop_index(op.f('ix_residents_active_job_id'), table_name='residents')
    op.drop_table('residents')
    _sqlite_foreign_keys(True)
