"""Operations: host commands users run against a run, such as a rerun or a cancel.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-24 12:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '0008'
down_revision: str | Sequence[str] | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name: str, nullable: bool) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=nullable)


def upgrade() -> None:
    op.create_table('operations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('action_id', sa.String(length=64), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('requested_by', sa.String(length=36), nullable=True),
    _ts('started_at', False),
    _ts('finished_at', True),
    sa.Column('exit_code', sa.Integer(), nullable=True),
    sa.Column('outcome', sa.String(length=16), nullable=True),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('log', sa.Text().with_variant(mysql.MEDIUMTEXT(), 'mysql'), nullable=True),
    sa.Column('active_run', sa.String(length=36), nullable=True),
    sa.Column('active_action', sa.String(length=64), nullable=True),
    sa.CheckConstraint("outcome IS NULL OR outcome IN ('ok', 'refused', 'failed')",
                       name=op.f('ck_operations_outcome')),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_operations_run_id_runs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_operations')),
    sa.UniqueConstraint('active_run', name=op.f('uq_operations_active_run')),
    sa.UniqueConstraint('active_action', name=op.f('uq_operations_active_action'))
    )
    op.create_index(op.f('ix_operations_run_id_started_at'), 'operations', ['run_id', 'started_at'])
    op.create_index(op.f('ix_operations_started_at'), 'operations', ['started_at'])


# MySQL refuses to drop an index its foreign key needs, so the indexes go with the table.
def downgrade() -> None:
    op.drop_table('operations')
