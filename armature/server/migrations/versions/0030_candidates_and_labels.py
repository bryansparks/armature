"""Candidates and labels: every finding a specialist raised, which decision it became, and versioned categories.

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-13 13:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '0030'
down_revision: str | Sequence[str] | None = '0029'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name: str, nullable: bool) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=nullable)


def upgrade() -> None:
    op.create_table('finding_candidates',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=True),
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('step_key', sa.String(length=191), nullable=False),
    sa.Column('specialist', sa.String(length=64), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('finding_key', sa.String(length=64), nullable=True),
    sa.Column('path', sa.Text(), nullable=True),
    sa.Column('line', sa.Integer(), nullable=True),
    sa.Column('severity', sa.String(length=16), nullable=True),
    sa.Column('blocking', sa.Boolean(), nullable=True),
    sa.Column('claim', sa.Text(), nullable=True),
    sa.Column('body', sa.Text(), nullable=True),
    sa.Column('suggestion', sa.Text(), nullable=True),
    sa.Column('arm_id', sa.String(length=36), nullable=True),
    _ts('created_at', False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_finding_candidates_project_id_projects')),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_finding_candidates_run_id_runs')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_finding_candidates_team_id_teams')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_finding_candidates')),
    sa.UniqueConstraint('run_id', 'step_key', 'ordinal', name=op.f('uq_finding_candidates_run_id_step_key_ordinal'))
    )
    op.create_index(op.f('ix_finding_candidates_team_id_created_at'), 'finding_candidates', ['team_id', 'created_at'],
                    unique=False)
    op.create_index(op.f('ix_finding_candidates_project_id_finding_key'), 'finding_candidates',
                    ['project_id', 'finding_key'], unique=False)
    op.create_table('candidate_matches',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('candidate_id', sa.String(length=36), nullable=False),
    sa.Column('finding_id', sa.String(length=36), nullable=True),
    sa.Column('match_ordinal', sa.Integer(), nullable=False),
    sa.Column('method', sa.String(length=16), nullable=False),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('matcher_version', sa.String(length=64), nullable=False),
    _ts('created_at', False),
    sa.ForeignKeyConstraint(['candidate_id'], ['finding_candidates.id'],
                            name=op.f('fk_candidate_matches_candidate_id_finding_candidates')),
    sa.ForeignKeyConstraint(['finding_id'], ['findings.id'], name=op.f('fk_candidate_matches_finding_id_findings')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_candidate_matches_team_id_teams')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_candidate_matches')),
    sa.UniqueConstraint('candidate_id', 'matcher_version', 'match_ordinal',
                        name=op.f('uq_candidate_matches_candidate_id_matcher_version_match_ordinal'))
    )
    op.create_index(op.f('ix_candidate_matches_finding_id'), 'candidate_matches', ['finding_id'], unique=False)
    op.create_table('taxonomies',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('version', sa.String(length=64), nullable=False),
    sa.Column('definition', sa.JSON(), nullable=False),
    _ts('created_at', False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_taxonomies_team_id_teams')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_taxonomies')),
    sa.UniqueConstraint('team_id', 'version', name=op.f('uq_taxonomies_team_id_version'))
    )
    op.create_table('labels',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('subject_kind', sa.String(length=16), nullable=False),
    sa.Column('subject_id', sa.String(length=64), nullable=False),
    sa.Column('taxonomy_version', sa.String(length=64), nullable=False),
    sa.Column('label', sa.String(length=191), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('labeler', sa.String(length=191), nullable=False),
    _ts('created_at', False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_labels_team_id_teams')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_labels')),
    sa.UniqueConstraint('team_id', 'subject_kind', 'subject_id', 'taxonomy_version',
                        name=op.f('uq_labels_team_id_subject_kind_subject_id_taxonomy_version'))
    )
    op.create_index(op.f('ix_labels_team_id_taxonomy_version_label'), 'labels',
                    ['team_id', 'taxonomy_version', 'label'], unique=False)
    op.create_table('clusters',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('taxonomy_version', sa.String(length=64), nullable=False),
    sa.Column('label', sa.String(length=191), nullable=False),
    sa.Column('scope', sa.JSON(), nullable=True),
    sa.Column('size', sa.Integer(), server_default='0', nullable=False),
    _ts('first_seen', True),
    _ts('last_seen', True),
    sa.Column('summary', sa.Text(), nullable=True),
    _ts('created_at', False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_clusters_team_id_teams')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_clusters'))
    )
    op.create_index(op.f('ix_clusters_team_id_taxonomy_version_label'), 'clusters',
                    ['team_id', 'taxonomy_version', 'label'], unique=False)
    op.create_table('cluster_members',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('cluster_id', sa.String(length=36), nullable=False),
    sa.Column('subject_kind', sa.String(length=16), nullable=False),
    sa.Column('subject_id', sa.String(length=64), nullable=False),
    _ts('created_at', False),
    sa.ForeignKeyConstraint(['cluster_id'], ['clusters.id'], name=op.f('fk_cluster_members_cluster_id_clusters')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_cluster_members_team_id_teams')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cluster_members')),
    sa.UniqueConstraint('cluster_id', 'subject_kind', 'subject_id',
                        name=op.f('uq_cluster_members_cluster_id_subject_kind_subject_id'))
    )


def downgrade() -> None:
    op.drop_table('cluster_members')
    op.drop_table('clusters')
    op.drop_table('labels')
    op.drop_table('taxonomies')
    op.drop_table('candidate_matches')
    op.drop_table('finding_candidates')
