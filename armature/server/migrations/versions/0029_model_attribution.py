"""Model attribution: the model, provider, tier, and effort each call actually used, one row per cost event.

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-13 12:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '0029'
down_revision: str | Sequence[str] | None = '0028'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name: str, nullable: bool) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=nullable)


def upgrade() -> None:
    op.create_table('llm_calls',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('seq', sa.BigInteger(), nullable=False),
    sa.Column('step_key', sa.String(length=191), nullable=True),
    sa.Column('model_id', sa.String(length=191), nullable=True),
    sa.Column('provider', sa.String(length=64), nullable=True),
    sa.Column('alias', sa.String(length=191), nullable=True),
    sa.Column('tier', sa.String(length=64), nullable=True),
    sa.Column('requested_effort', sa.String(length=16), nullable=True),
    sa.Column('reported_effort', sa.String(length=16), nullable=True),
    sa.Column('escalation_index', sa.Integer(), nullable=True),
    sa.Column('input_tokens', sa.BigInteger(), nullable=True),
    sa.Column('output_tokens', sa.BigInteger(), nullable=True),
    sa.Column('cost_usd', sa.Numeric(precision=14, scale=6), nullable=True),
    sa.Column('attempts', sa.JSON(), nullable=True),
    sa.Column('model_usage', sa.JSON(), nullable=True),
    sa.Column('cached', sa.Boolean(), server_default=sa.false(), nullable=False),
    _ts('created_at', False),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_llm_calls_run_id_runs')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_llm_calls_team_id_teams')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_llm_calls')),
    sa.UniqueConstraint('run_id', 'seq', name=op.f('uq_llm_calls_run_id_seq'))
    )
    op.create_index(op.f('ix_llm_calls_team_id_created_at'), 'llm_calls', ['team_id', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_table('llm_calls')
