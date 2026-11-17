"""GitHub sync: the files each merged pull request changed, a failure count for each sync state, and an unknown
review comment length.

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-13 20:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '0032'
down_revision: str | Sequence[str] | None = '0031'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('gh_pr_files',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('repo_row_id', sa.String(length=36), nullable=False),
    sa.Column('pr_row_id', sa.String(length=36), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('path_key', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"),
              nullable=False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_gh_pr_files_team_id_teams')),
    sa.ForeignKeyConstraint(['repo_row_id'], ['gh_repos.id'], name=op.f('fk_gh_pr_files_repo_row_id_gh_repos')),
    sa.ForeignKeyConstraint(['pr_row_id'], ['gh_pull_requests.id'],
                            name=op.f('fk_gh_pr_files_pr_row_id_gh_pull_requests')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_gh_pr_files')),
    sa.UniqueConstraint('pr_row_id', 'path_key', name=op.f('uq_gh_pr_files_pr_row_id_path_key'))
    )
    op.create_index(op.f('ix_gh_pr_files_team_id_repo_row_id_path_key'), 'gh_pr_files',
                    ['team_id', 'repo_row_id', 'path_key'], unique=False)
    with op.batch_alter_table('gh_sync_state') as batch:
        batch.add_column(sa.Column('failures', sa.Integer(), nullable=True))
    with op.batch_alter_table('gh_review_comments') as batch:
        batch.alter_column('body_length', existing_type=sa.Integer(), existing_server_default='0', nullable=True)


def downgrade() -> None:
    op.execute("UPDATE gh_review_comments SET body_length = 0 WHERE body_length IS NULL")
    with op.batch_alter_table('gh_review_comments') as batch:
        batch.alter_column('body_length', existing_type=sa.Integer(), existing_server_default='0', nullable=False)
    with op.batch_alter_table('gh_sync_state') as batch:
        batch.drop_column('failures')
    op.drop_index(op.f('ix_gh_pr_files_team_id_repo_row_id_path_key'), table_name='gh_pr_files')
    op.drop_table('gh_pr_files')
