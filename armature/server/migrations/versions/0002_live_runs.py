"""Live runs: runner keys, per-run event tokens, and the run and step columns the event writer fills.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-23 15:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '0002'
down_revision: str | Sequence[str] | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_COLUMNS = ("workflow_name", "actor", "trigger_ref", "runner_key_id", "error", "last_event_at")


# MySQL's DATETIME drops microseconds unless it is DATETIME(6).
def _timestamp():
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


# MySQL's default collation ignores case, so names use a binary one there.
def _name(length):
    return sa.String(length=length).with_variant(mysql.VARCHAR(length, collation="utf8mb4_bin"), "mysql")


def _run_steps(*key_columns, key):
    op.create_table('run_steps',
    sa.Column('run_id', sa.String(length=36), nullable=False),
    *key_columns[:2],
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('started_at', _timestamp(), nullable=True),
    sa.Column('finished_at', _timestamp(), nullable=True),
    sa.Column('cost_usd', sa.Numeric(precision=14, scale=6), nullable=True),
    sa.Column('input_tokens', sa.BigInteger(), nullable=True),
    sa.Column('output_tokens', sa.BigInteger(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('output', sa.JSON(), nullable=True),
    *key_columns[2:],
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_run_steps_run_id_runs')),
    sa.PrimaryKeyConstraint('run_id', key, name=op.f('pk_run_steps'))
    )


def upgrade() -> None:
    op.add_column('runs', sa.Column('workflow_name', sa.String(length=191), nullable=True))
    op.add_column('runs', sa.Column('actor', sa.String(length=191), nullable=True))
    op.add_column('runs', sa.Column('trigger_ref', sa.JSON(), nullable=True))
    op.add_column('runs', sa.Column('runner_key_id', sa.String(length=36), nullable=True))
    op.add_column('runs', sa.Column('error', sa.Text(), nullable=True))
    op.add_column('runs', sa.Column('last_event_at', _timestamp(), nullable=True))
    op.create_index(op.f('ix_runs_status'), 'runs', ['status'])
    # Nothing wrote run_steps before this revision, so it is rebuilt keyed by step_key.
    op.drop_table('run_steps')
    _run_steps(sa.Column('step_key', sa.String(length=191), nullable=False),
               sa.Column('step_id', sa.String(length=191), nullable=False),
               sa.Column('parent_step', sa.String(length=191), nullable=True),
               key='step_key')
    op.add_column('findings', sa.Column('title', sa.Text(), nullable=True))
    op.add_column('findings', sa.Column('step_id', sa.String(length=191), nullable=True))
    op.add_column('findings', sa.Column('created_at', _timestamp(), nullable=True))
    op.create_table('runner_keys',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('name', _name(191), nullable=False),
    sa.Column('key_hmac', sa.String(length=64), nullable=False),
    sa.Column('created_by', sa.String(length=36), nullable=True),
    sa.Column('created_at', _timestamp(), nullable=False),
    sa.Column('last_used_at', _timestamp(), nullable=True),
    sa.Column('revoked_at', _timestamp(), nullable=True),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_runner_keys_team_id_teams')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_runner_keys')),
    sa.UniqueConstraint('key_hmac', name=op.f('uq_runner_keys_key_hmac'))
    )
    op.create_index(op.f('ix_runner_keys_team_id'), 'runner_keys', ['team_id'])
    op.create_table('run_tokens',
    sa.Column('token_hmac', sa.String(length=64), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('expires_at', _timestamp(), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_run_tokens_run_id_runs')),
    sa.PrimaryKeyConstraint('token_hmac', name=op.f('pk_run_tokens'))
    )
    op.create_index(op.f('ix_run_tokens_run_id'), 'run_tokens', ['run_id'])


def downgrade() -> None:
    op.drop_table('run_tokens')
    op.drop_table('runner_keys')
    with op.batch_alter_table('findings') as batch:
        for name in ('title', 'step_id', 'created_at'):
            batch.drop_column(name)
    op.drop_table('run_steps')
    _run_steps(sa.Column('step_id', sa.String(length=191), nullable=False), key='step_id')
    op.drop_index(op.f('ix_runs_status'), table_name='runs')
    with op.batch_alter_table('runs') as batch:
        for name in RUN_COLUMNS:
            batch.drop_column(name)
