"""Tool calls: each built-in tool call a run's step made through the server's broker.

Revision ID: 0018
Revises: 0021
Create Date: 2026-10-28 18:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '0018'
down_revision: str | Sequence[str] | None = '0021'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The values armature.server.db.schema allowed at this revision, frozen here.
MODES = ('live', 'dry')
OUTCOMES = ('ok', 'approved', 'refused', 'failed')


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table('tool_calls',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=True),
    sa.Column('step_id', sa.String(length=191), nullable=False),
    sa.Column('tool', sa.String(length=64), nullable=False),
    sa.Column('mode', sa.String(length=8), nullable=False),
    sa.Column('outcome', sa.String(length=16), nullable=False),
    sa.Column('target', sa.String(length=191), nullable=True),
    sa.Column('detail', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), 'mysql'),
              nullable=False),
    sa.CheckConstraint(_in('mode', MODES), name=op.f('ck_tool_calls_mode')),
    sa.CheckConstraint(_in('outcome', OUTCOMES), name=op.f('ck_tool_calls_outcome')),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_tool_calls_run_id_runs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tool_calls'))
    )
    op.create_index(op.f('ix_tool_calls_run_id'), 'tool_calls', ['run_id'], unique=False)
    op.create_index(op.f('ix_tool_calls_tool_target_created_at'), 'tool_calls', ['tool', 'target', 'created_at'],
                    unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_tool_calls_tool_target_created_at'), table_name='tool_calls')
    op.drop_index(op.f('ix_tool_calls_run_id'), table_name='tool_calls')
    op.drop_table('tool_calls')
