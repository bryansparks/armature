"""GitHub history: tracked repositories, their pull requests, pushes, reviews, and checks, and the people behind them.

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-13 18:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '0031'
down_revision: str | Sequence[str] | None = '0030'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PR_STATES = ('open', 'closed', 'merged')
PERSON_KINDS = ('human', 'bot')
IDENTITY_KINDS = ('github_id', 'github_login', 'email', 'jira_account', 'armature')
PUSH_SOURCES = ('webhook', 'sync')
ACTOR_SOURCES = ('github', 'armature')
LINK_KINDS = ('revert', 'fix')
MERGEABLE = ('mergeable', 'conflicting', 'unknown')
SYNC_KINDS = ('backfill', 'poll', 'conflicts', 'gap_fill')


def _ts(name: str, nullable: bool) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=nullable)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ck(table: str, column: str, values: tuple[str, ...]) -> sa.CheckConstraint:
    return sa.CheckConstraint(_in(column, values), name=op.f(f'ck_{table}_{column}'))


def _team(table: str) -> list:
    return [sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('team_id', sa.String(length=36), nullable=False),
            sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f(f'fk_{table}_team_id_teams')),
            sa.PrimaryKeyConstraint('id', name=op.f(f'pk_{table}'))]


def _fk(table: str, column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [f'{target}.id'], name=op.f(f'fk_{table}_{column}_{target}'))


def upgrade() -> None:
    op.create_table('gh_repos', *_team('gh_repos'),
    sa.Column('gh_repo_id', sa.BigInteger(), nullable=False),
    sa.Column('full_name', sa.String(length=191), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=True),
    sa.Column('connection_id', sa.String(length=36), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('backfill_since', sa.Date(), nullable=True),
    _ts('created_at', False),
    _ts('updated_at', False),
    _fk('gh_repos', 'project_id', 'projects'),
    _fk('gh_repos', 'connection_id', 'connections'),
    sa.UniqueConstraint('team_id', 'gh_repo_id', name=op.f('uq_gh_repos_team_id_gh_repo_id')),
    sa.UniqueConstraint('team_id', 'full_name', name=op.f('uq_gh_repos_team_id_full_name'))
    )
    op.create_table('people', *_team('people'),
    sa.Column('display_name', sa.String(length=191), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('user_id', sa.String(length=36), nullable=True),
    _ts('created_at', False),
    _ts('updated_at', False),
    _fk('people', 'user_id', 'users'),
    _ck('people', 'kind', PERSON_KINDS)
    )
    op.create_table('person_identities', *_team('person_identities'),
    sa.Column('person_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('value', sa.String(length=191), nullable=False),
    sa.Column('verified', sa.Boolean(), server_default=sa.false(), nullable=False),
    _ts('first_seen', False),
    _ts('last_seen', False),
    _fk('person_identities', 'person_id', 'people'),
    sa.UniqueConstraint('team_id', 'kind', 'value', name=op.f('uq_person_identities_team_id_kind_value')),
    _ck('person_identities', 'kind', IDENTITY_KINDS)
    )
    op.create_index(op.f('ix_person_identities_person_id'), 'person_identities', ['person_id'], unique=False)
    op.create_table('gh_pull_requests', *_team('gh_pull_requests'),
    sa.Column('repo_row_id', sa.String(length=36), nullable=False),
    sa.Column('gh_pr_id', sa.BigInteger(), nullable=False),
    sa.Column('number', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=512), nullable=True),
    sa.Column('author_person_id', sa.String(length=36), nullable=True),
    sa.Column('author_gh_id', sa.BigInteger(), nullable=False),
    sa.Column('author_login', sa.String(length=191), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('draft', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('base_ref', sa.String(length=255), nullable=True),
    sa.Column('head_ref', sa.String(length=255), nullable=True),
    sa.Column('head_repo_gh_id', sa.BigInteger(), nullable=True),
    sa.Column('head_sha', sa.String(length=64), nullable=True),
    sa.Column('base_sha', sa.String(length=64), nullable=True),
    sa.Column('merge_commit_sha', sa.String(length=64), nullable=True),
    sa.Column('merge_method', sa.String(length=16), nullable=True),
    sa.Column('additions', sa.Integer(), nullable=True),
    sa.Column('deletions', sa.Integer(), nullable=True),
    sa.Column('changed_files', sa.Integer(), nullable=True),
    _ts('created_at_gh', False),
    _ts('closed_at_gh', True),
    _ts('merged_at_gh', True),
    sa.Column('merged_by_gh_id', sa.BigInteger(), nullable=True),
    _ts('gh_updated_at', False),
    sa.Column('jira_key', sa.String(length=64), nullable=True),
    _ts('created_at', False),
    _ts('updated_at', False),
    _fk('gh_pull_requests', 'repo_row_id', 'gh_repos'),
    _fk('gh_pull_requests', 'author_person_id', 'people'),
    sa.UniqueConstraint('team_id', 'gh_pr_id', name=op.f('uq_gh_pull_requests_team_id_gh_pr_id')),
    _ck('gh_pull_requests', 'state', PR_STATES)
    )
    op.create_index(op.f('ix_gh_pull_requests_team_id_repo_row_id_number'), 'gh_pull_requests',
                    ['team_id', 'repo_row_id', 'number'], unique=False)
    op.create_index(op.f('ix_gh_pull_requests_team_id_author_person_id_created_at_gh'), 'gh_pull_requests',
                    ['team_id', 'author_person_id', 'created_at_gh'], unique=False)
    op.create_index(op.f('ix_gh_pull_requests_team_id_state'), 'gh_pull_requests', ['team_id', 'state'],
                    unique=False)
    op.create_table('gh_pushes', *_team('gh_pushes'),
    sa.Column('pr_row_id', sa.String(length=36), nullable=False),
    sa.Column('before_sha', sa.String(length=64), nullable=False),
    sa.Column('after_sha', sa.String(length=64), nullable=False),
    sa.Column('base_sha', sa.String(length=64), nullable=True),
    sa.Column('forced', sa.Boolean(), nullable=True),
    sa.Column('pusher_gh_id', sa.BigInteger(), nullable=True),
    sa.Column('pusher_login', sa.String(length=191), nullable=True),
    sa.Column('pusher_person_id', sa.String(length=36), nullable=True),
    sa.Column('actor_source', sa.String(length=16), server_default='github', nullable=False),
    _ts('pushed_at', False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('push_class', sa.String(length=32), nullable=True),
    sa.Column('class_confidence', sa.Float(), nullable=True),
    sa.Column('class_evidence', sa.JSON(), nullable=True),
    sa.Column('classifier_version', sa.String(length=64), nullable=True),
    _ts('created_at', False),
    _fk('gh_pushes', 'pr_row_id', 'gh_pull_requests'),
    _fk('gh_pushes', 'pusher_person_id', 'people'),
    sa.UniqueConstraint('pr_row_id', 'after_sha', name=op.f('uq_gh_pushes_pr_row_id_after_sha')),
    _ck('gh_pushes', 'source', PUSH_SOURCES),
    _ck('gh_pushes', 'actor_source', ACTOR_SOURCES)
    )
    op.create_index(op.f('ix_gh_pushes_team_id_push_class'), 'gh_pushes', ['team_id', 'push_class'], unique=False)
    op.create_table('gh_reviews', *_team('gh_reviews'),
    sa.Column('pr_row_id', sa.String(length=36), nullable=False),
    sa.Column('gh_review_id', sa.BigInteger(), nullable=False),
    sa.Column('reviewer_person_id', sa.String(length=36), nullable=True),
    sa.Column('reviewer_gh_id', sa.BigInteger(), nullable=False),
    sa.Column('reviewer_login', sa.String(length=191), nullable=False),
    sa.Column('actor_source', sa.String(length=16), server_default='github', nullable=False),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('head_sha', sa.String(length=64), nullable=True),
    _ts('submitted_at', True),
    sa.Column('body_length', sa.Integer(), server_default='0', nullable=False),
    sa.Column('comment_count', sa.Integer(), nullable=True),
    sa.Column('url', sa.String(length=512), nullable=True),
    _ts('created_at', False),
    _ts('updated_at', False),
    _fk('gh_reviews', 'pr_row_id', 'gh_pull_requests'),
    _fk('gh_reviews', 'reviewer_person_id', 'people'),
    sa.UniqueConstraint('team_id', 'gh_review_id', name=op.f('uq_gh_reviews_team_id_gh_review_id')),
    _ck('gh_reviews', 'actor_source', ACTOR_SOURCES)
    )
    op.create_index(op.f('ix_gh_reviews_pr_row_id'), 'gh_reviews', ['pr_row_id'], unique=False)
    op.create_table('gh_review_comments', *_team('gh_review_comments'),
    sa.Column('pr_row_id', sa.String(length=36), nullable=False),
    sa.Column('gh_comment_id', sa.BigInteger(), nullable=False),
    sa.Column('gh_review_id', sa.BigInteger(), nullable=True),
    sa.Column('author_person_id', sa.String(length=36), nullable=True),
    sa.Column('author_gh_id', sa.BigInteger(), nullable=False),
    sa.Column('author_login', sa.String(length=191), nullable=False),
    sa.Column('path', sa.Text(), nullable=True),
    sa.Column('line', sa.Integer(), nullable=True),
    sa.Column('head_sha', sa.String(length=64), nullable=True),
    _ts('created_at_gh', False),
    _ts('updated_at_gh', False),
    sa.Column('deleted', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('body_length', sa.Integer(), server_default='0', nullable=False),
    sa.Column('url', sa.String(length=512), nullable=True),
    sa.Column('in_reply_to', sa.BigInteger(), nullable=True),
    _ts('created_at', False),
    _fk('gh_review_comments', 'pr_row_id', 'gh_pull_requests'),
    _fk('gh_review_comments', 'author_person_id', 'people'),
    sa.UniqueConstraint('team_id', 'gh_comment_id', name=op.f('uq_gh_review_comments_team_id_gh_comment_id'))
    )
    op.create_index(op.f('ix_gh_review_comments_pr_row_id'), 'gh_review_comments', ['pr_row_id'], unique=False)
    op.create_table('gh_check_runs', *_team('gh_check_runs'),
    sa.Column('pr_row_id', sa.String(length=36), nullable=True),
    sa.Column('gh_check_run_id', sa.BigInteger(), nullable=False),
    sa.Column('repo_row_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=191), nullable=False),
    sa.Column('head_sha', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('conclusion', sa.String(length=32), nullable=True),
    _ts('started_at', True),
    _ts('completed_at', True),
    _ts('created_at', False),
    _fk('gh_check_runs', 'pr_row_id', 'gh_pull_requests'),
    _fk('gh_check_runs', 'repo_row_id', 'gh_repos'),
    sa.UniqueConstraint('team_id', 'gh_check_run_id', name=op.f('uq_gh_check_runs_team_id_gh_check_run_id'))
    )
    op.create_index(op.f('ix_gh_check_runs_team_id_repo_row_id_head_sha'), 'gh_check_runs',
                    ['team_id', 'repo_row_id', 'head_sha'], unique=False)
    op.create_index(op.f('ix_gh_check_runs_pr_row_id'), 'gh_check_runs', ['pr_row_id'], unique=False)
    op.create_table('gh_pr_commits', *_team('gh_pr_commits'),
    sa.Column('repo_row_id', sa.String(length=36), nullable=False),
    sa.Column('commit_sha', sa.String(length=64), nullable=False),
    sa.Column('pr_row_id', sa.String(length=36), nullable=False),
    _ts('created_at', False),
    _fk('gh_pr_commits', 'repo_row_id', 'gh_repos'),
    _fk('gh_pr_commits', 'pr_row_id', 'gh_pull_requests'),
    sa.UniqueConstraint('team_id', 'repo_row_id', 'commit_sha',
                        name=op.f('uq_gh_pr_commits_team_id_repo_row_id_commit_sha'))
    )
    op.create_index(op.f('ix_gh_pr_commits_pr_row_id'), 'gh_pr_commits', ['pr_row_id'], unique=False)
    op.create_table('gh_pr_links', *_team('gh_pr_links'),
    sa.Column('from_pr_row_id', sa.String(length=36), nullable=False),
    sa.Column('to_pr_row_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('detector_version', sa.String(length=64), nullable=False),
    _ts('created_at', False),
    _fk('gh_pr_links', 'from_pr_row_id', 'gh_pull_requests'),
    _fk('gh_pr_links', 'to_pr_row_id', 'gh_pull_requests'),
    sa.UniqueConstraint('from_pr_row_id', 'to_pr_row_id', 'kind',
                        name=op.f('uq_gh_pr_links_from_pr_row_id_to_pr_row_id_kind')),
    _ck('gh_pr_links', 'kind', LINK_KINDS)
    )
    op.create_index(op.f('ix_gh_pr_links_to_pr_row_id'), 'gh_pr_links', ['to_pr_row_id'], unique=False)
    op.create_table('gh_conflict_samples', *_team('gh_conflict_samples'),
    sa.Column('pr_row_id', sa.String(length=36), nullable=False),
    _ts('sampled_at', False),
    sa.Column('mergeable', sa.String(length=16), nullable=False),
    sa.Column('merge_state', sa.String(length=32), nullable=True),
    _fk('gh_conflict_samples', 'pr_row_id', 'gh_pull_requests'),
    sa.UniqueConstraint('pr_row_id', 'sampled_at', name=op.f('uq_gh_conflict_samples_pr_row_id_sampled_at')),
    _ck('gh_conflict_samples', 'mergeable', MERGEABLE)
    )
    op.create_table('gh_sync_state', *_team('gh_sync_state'),
    sa.Column('repo_row_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('cursor', sa.Text(), nullable=True),
    _ts('watermark', True),
    _ts('next_run_at', True),
    sa.Column('last_error', sa.Text(), nullable=True),
    _ts('updated_at', False),
    _fk('gh_sync_state', 'repo_row_id', 'gh_repos'),
    sa.UniqueConstraint('team_id', 'repo_row_id', 'kind', name=op.f('uq_gh_sync_state_team_id_repo_row_id_kind')),
    _ck('gh_sync_state', 'kind', SYNC_KINDS)
    )


def downgrade() -> None:
    op.drop_table('gh_sync_state')
    op.drop_table('gh_conflict_samples')
    op.drop_table('gh_pr_links')
    op.drop_table('gh_pr_commits')
    op.drop_table('gh_check_runs')
    op.drop_table('gh_review_comments')
    op.drop_table('gh_reviews')
    op.drop_table('gh_pushes')
    op.drop_table('gh_pull_requests')
    op.drop_table('person_identities')
    op.drop_table('people')
    op.drop_table('gh_repos')
