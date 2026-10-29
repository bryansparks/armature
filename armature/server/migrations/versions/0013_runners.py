"""Runners: the runners table, what a job requires and who runs it, cancel requests, and runner key kinds.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-28 09:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '0013'
down_revision: str | Sequence[str] | None = '0012'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = "kind IN ('standing', 'ephemeral')"


def _ts(name: str, nullable: bool) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=nullable)


# As in 0012: SQLite rebuilds jobs and runner_keys, and other tables reference them.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def upgrade() -> None:
    _sqlite_foreign_keys(False)
    # Keys and jobs that exist already are standing keys and workflow jobs, filled in by the server defaults.
    with op.batch_alter_table('runner_keys') as batch:
        batch.add_column(sa.Column('kind', sa.String(length=16), nullable=False, server_default='standing'))
        batch.add_column(_ts('expires_at', True))
        batch.add_column(sa.Column('capabilities', sa.JSON(), nullable=True))
        batch.create_check_constraint(op.f('ck_runner_keys_kind'), KINDS)
    op.create_table('runners',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('runner_key_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=191).with_variant(mysql.VARCHAR(length=191, collation='utf8mb4_bin'), 'mysql'),
              nullable=False),
    sa.Column('capabilities', sa.JSON(), nullable=True),
    sa.Column('labels', sa.JSON(), nullable=True),
    sa.Column('version', sa.String(length=64), nullable=True),
    sa.Column('host', sa.String(length=191), nullable=True),
    sa.Column('slots', sa.Integer(), nullable=True),
    sa.Column('state', sa.String(length=16), nullable=False),
    _ts('created_at', False),
    _ts('last_seen_at', False),
    sa.ForeignKeyConstraint(['runner_key_id'], ['runner_keys.id'], name=op.f('fk_runners_runner_key_id_runner_keys')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_runners')),
    sa.UniqueConstraint('runner_key_id', 'name', name=op.f('uq_runners_runner_key_id_name'))
    )
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('kind', sa.String(length=16), nullable=False, server_default='workflow'))
        batch.add_column(sa.Column('requires', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('runner_id', sa.String(length=36), nullable=True))
        batch.add_column(_ts('cancel_requested_at', True))
        batch.create_foreign_key(op.f('fk_jobs_runner_id_runners'), 'runners', ['runner_id'], ['id'])
        batch.create_index(op.f('ix_jobs_runner_id'), ['runner_id'])
    _sqlite_foreign_keys(True)


def downgrade() -> None:
    _sqlite_foreign_keys(False)
    # The old schema has no canceled job and no reason a held job would stop, so both read as failed.
    op.execute("UPDATE jobs SET status = 'failed' WHERE status IN ('canceled', 'provisioning')")
    with op.batch_alter_table('jobs') as batch:
        batch.drop_index(op.f('ix_jobs_runner_id'))
        batch.drop_constraint(op.f('fk_jobs_runner_id_runners'), type_='foreignkey')
        batch.drop_column('cancel_requested_at')
        batch.drop_column('runner_id')
        batch.drop_column('requires')
        batch.drop_column('kind')
    op.drop_table('runners')
    # Ephemeral keys belong to environments the old schema can't tear down, so they go with their tokens.
    ephemeral = "(SELECT id FROM runner_keys WHERE kind = 'ephemeral')"
    op.execute(f"DELETE FROM run_tokens WHERE run_id IN (SELECT id FROM runs WHERE runner_key_id IN {ephemeral})")
    op.execute(f"UPDATE runner_keys SET revoked_at = CURRENT_TIMESTAMP WHERE kind = 'ephemeral' AND revoked_at IS NULL")
    with op.batch_alter_table('runner_keys') as batch:
        batch.drop_constraint(op.f('ck_runner_keys_kind'), type_='check')
        batch.drop_column('capabilities')
        batch.drop_column('expires_at')
        batch.drop_column('kind')
    _sqlite_foreign_keys(True)
