"""Initial schema: every table in DESIGN.md, "Data model".

Revision ID: 0001
Revises:
Create Date: 2026-10-23 10:08:23.580819

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# MySQL's DATETIME drops microseconds unless it is DATETIME(6).
def _timestamp():
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


# MySQL's default collation ignores case, so names use a binary one there.
def _name(length):
    return sa.String(length=length).with_variant(mysql.VARCHAR(length, collation="utf8mb4_bin"), "mysql")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('audit',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('at', _timestamp(), nullable=False),
    sa.Column('actor_id', sa.String(length=36), nullable=True),
    sa.Column('team_id', sa.String(length=36), nullable=True),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('target_kind', sa.String(length=32), nullable=True),
    sa.Column('target_id', sa.String(length=191), nullable=True),
    sa.Column('detail', sa.JSON(), nullable=True),
    sa.Column('ip', sa.String(length=45), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit'))
    )
    op.create_index(op.f('ix_audit_at'), 'audit', ['at'])
    op.create_index(op.f('ix_audit_team_id_at'), 'audit', ['team_id', 'at'])
    op.create_table('login_attempts',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('username', sa.String(length=191), nullable=True),
    sa.Column('ip', sa.String(length=45), nullable=True),
    sa.Column('succeeded', sa.Boolean(), nullable=False),
    sa.Column('at', _timestamp(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_login_attempts'))
    )
    op.create_index(op.f('ix_login_attempts_ip_at'), 'login_attempts', ['ip', 'at'])
    op.create_index(op.f('ix_login_attempts_username_at'), 'login_attempts', ['username', 'at'])
    op.create_table('packs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=191), nullable=False),
    sa.Column('version', sa.String(length=64), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('pinned_ref', sa.String(length=191), nullable=False),
    sa.Column('installed_by', sa.String(length=36), nullable=True),
    sa.Column('installed_at', _timestamp(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_packs')),
    sa.UniqueConstraint('name', name=op.f('uq_packs_name'))
    )
    op.create_table('run_events',
    sa.Column('run_id', sa.String(length=36), autoincrement=False, nullable=False),
    sa.Column('seq', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('ts', _timestamp(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('step_id', sa.String(length=191), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('run_id', 'seq', name=op.f('pk_run_events'))
    )
    op.create_table('settings',
    sa.Column('key', sa.String(length=191), nullable=False),
    sa.Column('value', sa.JSON(), nullable=True),
    sa.Column('updated_at', _timestamp(), nullable=True),
    sa.Column('updated_by', sa.String(length=36), nullable=True),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_settings'))
    )
    op.create_table('teams',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', _name(191), nullable=False),
    sa.Column('created_at', _timestamp(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_teams')),
    sa.UniqueConstraint('name', name=op.f('uq_teams_name'))
    )
    op.create_table('users',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=True),
    sa.Column('display_name', sa.String(length=191), nullable=True),
    sa.Column('email', sa.String(length=191), nullable=True),
    sa.Column('oidc_iss', sa.String(length=191), nullable=True),
    sa.Column('oidc_sub', sa.String(length=191), nullable=True),
    sa.Column('password_hash', sa.String(length=255), nullable=True),
    sa.Column('totp_secret_enc', sa.Text(), nullable=True),
    sa.Column('last_totp_step', sa.BigInteger(), nullable=False),
    sa.Column('is_admin', sa.Boolean(), nullable=False),
    sa.Column('disabled', sa.Boolean(), nullable=False),
    sa.Column('totp_confirmed_at', _timestamp(), nullable=True),
    sa.Column('created_at', _timestamp(), nullable=False),
    sa.Column('last_login_at', _timestamp(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('oidc_iss', 'oidc_sub', name=op.f('uq_users_oidc_iss_oidc_sub')),
    sa.UniqueConstraint('username', name=op.f('uq_users_username'))
    )
    op.create_table('versions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('object_kind', sa.String(length=16), nullable=False),
    sa.Column('object_id', sa.String(length=36), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('body', sa.Text().with_variant(mysql.MEDIUMTEXT(), 'mysql'), nullable=False),
    sa.Column('author_id', sa.String(length=36), nullable=True),
    sa.Column('created_at', _timestamp(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_versions')),
    sa.UniqueConstraint('object_kind', 'object_id', 'version', name=op.f('uq_versions_object_kind_object_id_version'))
    )
    op.create_table('documents',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=True),
    sa.Column('scope', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('key', _name(191), nullable=False),
    sa.Column('current_version', sa.Integer(), nullable=False),
    sa.Column('created_at', _timestamp(), nullable=False),
    sa.Column('updated_at', _timestamp(), nullable=False),
    sa.CheckConstraint("(team_id IS NULL AND scope = '*') OR (team_id IS NOT NULL AND scope = team_id)", name=op.f('ck_documents_scope')),
    sa.CheckConstraint("kind IN ('role', 'prompt', 'mission')", name=op.f('ck_documents_kind')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_documents_team_id_teams')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_documents')),
    sa.UniqueConstraint('scope', 'kind', 'key', name=op.f('uq_documents_scope_kind_key'))
    )
    op.create_table('memberships',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.String(length=36), nullable=False),
    sa.Column('scope_kind', sa.String(length=16), nullable=False),
    sa.Column('scope_id', sa.String(length=36), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('source', sa.String(length=16), server_default='manual', nullable=False),
    sa.Column('created_at', _timestamp(), nullable=False),
    sa.CheckConstraint("role IN ('admin', 'team_lead', 'project_lead', 'contributor', 'viewer')", name=op.f('ck_memberships_role')),
    sa.CheckConstraint("scope_kind IN ('team', 'project')", name=op.f('ck_memberships_scope_kind')),
    sa.CheckConstraint("source IN ('manual', 'oidc')", name=op.f('ck_memberships_source')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_memberships_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_memberships')),
    sa.UniqueConstraint('user_id', 'scope_kind', 'scope_id', name=op.f('uq_memberships_user_id_scope_kind_scope_id'))
    )
    op.create_index(op.f('ix_memberships_scope_kind_scope_id'), 'memberships', ['scope_kind', 'scope_id'])
    op.create_table('projects',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('name', _name(191), nullable=False),
    sa.Column('platform', sa.String(length=32), nullable=True),
    sa.Column('enabled_workflows', sa.JSON(), nullable=True),
    sa.Column('branch_filters', sa.JSON(), nullable=True),
    sa.Column('created_at', _timestamp(), nullable=False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_projects_team_id_teams')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_projects')),
    sa.UniqueConstraint('team_id', 'name', name=op.f('uq_projects_team_id_name'))
    )
    op.create_table('providers',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('config', sa.JSON(), nullable=True),
    sa.Column('aliases', sa.JSON(), nullable=True),
    sa.Column('created_at', _timestamp(), nullable=False),
    sa.Column('updated_at', _timestamp(), nullable=False),
    sa.CheckConstraint("kind IN ('bedrock', 'anthropic', 'claude-subscription', 'openai-compatible', 'ollama')", name=op.f('ck_providers_kind')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_providers_team_id_teams')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_providers'))
    )
    op.create_index(op.f('ix_providers_team_id'), 'providers', ['team_id'])
    op.create_table('secrets',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=True),
    sa.Column('scope', sa.String(length=36), nullable=False),
    sa.Column('name', _name(128), nullable=False),
    sa.Column('ciphertext', sa.Text(), nullable=False),
    sa.Column('key_id', sa.String(length=8), nullable=False),
    sa.Column('updated_at', _timestamp(), nullable=False),
    sa.Column('updated_by', sa.String(length=36), nullable=True),
    sa.CheckConstraint("(team_id IS NULL AND scope = '*') OR (team_id IS NOT NULL AND scope = team_id)", name=op.f('ck_secrets_scope')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_secrets_team_id_teams')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_secrets')),
    sa.UniqueConstraint('scope', 'name', name=op.f('uq_secrets_scope_name'))
    )
    op.create_table('invites',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('token_hmac', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=36), nullable=False),
    sa.Column('created_by', sa.String(length=36), nullable=True),
    sa.Column('created_at', _timestamp(), nullable=False),
    sa.Column('expires_at', _timestamp(), nullable=False),
    sa.Column('used_at', _timestamp(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_invites_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_invites')),
    sa.UniqueConstraint('token_hmac', name=op.f('uq_invites_token_hmac'))
    )
    op.create_index(op.f('ix_invites_user_id'), 'invites', ['user_id'])
    op.create_table('sessions',
    sa.Column('token_hmac', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=36), nullable=False),
    sa.Column('csrf', sa.String(length=64), nullable=False),
    sa.Column('created_at', _timestamp(), nullable=False),
    sa.Column('last_seen_at', _timestamp(), nullable=False),
    sa.Column('step_up_at', _timestamp(), nullable=True),
    sa.Column('ip', sa.String(length=45), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_sessions_user_id_users')),
    sa.PrimaryKeyConstraint('token_hmac', name=op.f('pk_sessions'))
    )
    op.create_index(op.f('ix_sessions_user_id'), 'sessions', ['user_id'])
    op.create_table('workflows',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('name', _name(191), nullable=False),
    sa.Column('current_version', sa.Integer(), nullable=False),
    sa.Column('created_at', _timestamp(), nullable=False),
    sa.Column('updated_at', _timestamp(), nullable=False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_workflows_team_id_teams')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_workflows')),
    sa.UniqueConstraint('team_id', 'name', name=op.f('uq_workflows_team_id_name'))
    )
    op.create_table('runs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=True),
    sa.Column('workflow_id', sa.String(length=36), nullable=True),
    sa.Column('workflow_version', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('triggered_by', sa.String(length=36), nullable=True),
    sa.Column('trigger_kind', sa.String(length=32), nullable=True),
    sa.Column('cancelled_by', sa.String(length=36), nullable=True),
    sa.Column('rerun_of', sa.String(length=36), nullable=True),
    sa.Column('started_at', _timestamp(), nullable=False),
    sa.Column('finished_at', _timestamp(), nullable=True),
    sa.Column('cost_usd', sa.Numeric(precision=14, scale=6), nullable=True),
    sa.Column('input_tokens', sa.BigInteger(), nullable=True),
    sa.Column('output_tokens', sa.BigInteger(), nullable=True),
    sa.Column('summary', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_runs_project_id_projects')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_runs_team_id_teams')),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], name=op.f('fk_runs_workflow_id_workflows')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_runs'))
    )
    op.create_index(op.f('ix_runs_team_id_started_at'), 'runs', ['team_id', 'started_at'])
    op.create_table('triggers',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('workflow_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('config', sa.JSON(), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', _timestamp(), nullable=False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_triggers_team_id_teams')),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], name=op.f('fk_triggers_workflow_id_workflows')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_triggers'))
    )
    op.create_index(op.f('ix_triggers_workflow_id'), 'triggers', ['workflow_id'])
    op.create_table('findings',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=True),
    sa.Column('finding_key', sa.String(length=64), nullable=False),
    sa.Column('path', sa.Text(), nullable=True),
    sa.Column('line', sa.Integer(), nullable=True),
    sa.Column('severity', sa.String(length=16), nullable=True),
    sa.Column('disposition', sa.String(length=32), nullable=True),
    sa.Column('body', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_findings_project_id_projects')),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_findings_run_id_runs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_findings'))
    )
    op.create_index(op.f('ix_findings_project_id_finding_key'), 'findings', ['project_id', 'finding_key'])
    op.create_index(op.f('ix_findings_run_id'), 'findings', ['run_id'])
    op.create_table('jobs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=True),
    sa.Column('workflow_id', sa.String(length=36), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=True),
    sa.Column('inputs', sa.JSON(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('claimed_by', sa.String(length=191), nullable=True),
    sa.Column('claimed_at', _timestamp(), nullable=True),
    sa.Column('heartbeat_at', _timestamp(), nullable=True),
    sa.Column('lease_expires_at', _timestamp(), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('created_at', _timestamp(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_jobs_project_id_projects')),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_jobs_run_id_runs')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_jobs_team_id_teams')),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], name=op.f('fk_jobs_workflow_id_workflows')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_jobs'))
    )
    op.create_index(op.f('ix_jobs_status_created_at'), 'jobs', ['status', 'created_at'])
    op.create_table('run_steps',
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('step_id', sa.String(length=191), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('started_at', _timestamp(), nullable=True),
    sa.Column('finished_at', _timestamp(), nullable=True),
    sa.Column('cost_usd', sa.Numeric(precision=14, scale=6), nullable=True),
    sa.Column('input_tokens', sa.BigInteger(), nullable=True),
    sa.Column('output_tokens', sa.BigInteger(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('output', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_run_steps_run_id_runs')),
    sa.PrimaryKeyConstraint('run_id', 'step_id', name=op.f('pk_run_steps'))
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('run_steps')
    op.drop_table('jobs')
    op.drop_table('findings')
    op.drop_table('triggers')
    op.drop_table('runs')
    op.drop_table('workflows')
    op.drop_table('sessions')
    op.drop_table('invites')
    op.drop_table('secrets')
    op.drop_table('providers')
    op.drop_table('projects')
    op.drop_table('memberships')
    op.drop_table('documents')
    op.drop_table('versions')
    op.drop_table('users')
    op.drop_table('teams')
    op.drop_table('settings')
    op.drop_table('run_events')
    op.drop_table('packs')
    op.drop_table('login_attempts')
    op.drop_table('audit')
