"""Self-healing: the fixes Armature proposes for patterns review keeps flagging.

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-13 23:30:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '0034'
down_revision: str | Sequence[str] | None = '0033'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATES = ('queued', 'drafting', 'waiting', 'approved', 'declined', 'expired', 'opened', 'merged', 'closed', 'failed')
KINDS = ('check', 'sweep', 'guidance', 'author_note')


def _ts(name: str, nullable: bool) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=nullable)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table('fix_proposals',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('repo_row_id', sa.String(length=36), nullable=True),
    sa.Column('project_id', sa.String(length=36), nullable=True),
    sa.Column('cluster_key', sa.String(length=191), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=True),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=True),
    sa.Column('evidence', sa.JSON(), nullable=True),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=True),
    sa.Column('gate_id', sa.String(length=36), nullable=True),
    sa.Column('job_id', sa.String(length=36), nullable=True),
    sa.Column('branch', sa.String(length=255), nullable=True),
    sa.Column('base', sa.String(length=255), nullable=True),
    sa.Column('base_sha', sa.String(length=40), nullable=True),
    sa.Column('patch', sa.Text().with_variant(mysql.MEDIUMTEXT(), 'mysql'), nullable=True),
    sa.Column('diff_preview', sa.JSON(), nullable=True),
    sa.Column('risk', sa.String(length=16), nullable=True),
    sa.Column('pr_number', sa.Integer(), nullable=True),
    sa.Column('pr_url', sa.Text(), nullable=True),
    sa.Column('decided_by', sa.String(length=36), nullable=True),
    _ts('decided_at', True),
    sa.Column('decline_reason', sa.Text(), nullable=True),
    _ts('merged_at', True),
    sa.Column('flags_before', sa.Integer(), nullable=True),
    sa.Column('cost_usd', sa.Numeric(precision=14, scale=6), nullable=True),
    sa.Column('failure_reason', sa.Text(), nullable=True),
    _ts('created_at', False),
    _ts('updated_at', False),
    sa.CheckConstraint(_in('state', STATES), name=op.f('ck_fix_proposals_state')),
    sa.CheckConstraint(_in('kind', KINDS), name=op.f('ck_fix_proposals_kind')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_fix_proposals_team_id_teams')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_fix_proposals_project_id_projects')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_fix_proposals'))
    )
    op.create_index(op.f('ix_fix_proposals_team_id_state'), 'fix_proposals', ['team_id', 'state'], unique=False)
    op.create_index(op.f('ix_fix_proposals_team_id_cluster_key'), 'fix_proposals', ['team_id', 'cluster_key'],
                    unique=False)
    op.create_index(op.f('ix_fix_proposals_run_id'), 'fix_proposals', ['run_id'], unique=False)
    op.create_index(op.f('ix_fix_proposals_job_id'), 'fix_proposals', ['job_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_fix_proposals_job_id'), table_name='fix_proposals')
    op.drop_index(op.f('ix_fix_proposals_run_id'), table_name='fix_proposals')
    op.drop_index(op.f('ix_fix_proposals_team_id_cluster_key'), table_name='fix_proposals')
    op.drop_index(op.f('ix_fix_proposals_team_id_state'), table_name='fix_proposals')
    op.drop_table('fix_proposals')
