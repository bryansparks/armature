"""Connections: sealed GitHub App, Slack, Jira, and Linear credentials, and webhooks that verify with them.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-31 09:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '0016'
down_revision: str | Sequence[str] | None = '0015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The values armature.server.db.schema allowed at this revision, frozen here.
KINDS = ('github_app', 'slack', 'jira', 'linear')
STATES = ('unverified', 'ok', 'error')
SCOPE_MATCHES_TEAM = "(team_id IS NULL AND scope = '*') OR (team_id IS NOT NULL AND scope = team_id)"
SCOPE_MATCHES_DEFINITION = (f"(project_id IS NULL AND ({SCOPE_MATCHES_TEAM})) "
                            "OR (project_id IS NOT NULL AND team_id IS NOT NULL AND scope = project_id)")
WEBHOOK_SECRET = "secret_name IS NOT NULL OR connection_id IS NOT NULL"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ts(name: str, nullable: bool) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=nullable)


def _name(length: int) -> sa.String:
    return sa.String(length=length).with_variant(mysql.VARCHAR(length=length, collation='utf8mb4_bin'), 'mysql')


# As in 0015: SQLite rebuilds webhooks, and other tables reference it.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def upgrade() -> None:
    _sqlite_foreign_keys(False)
    op.create_table('connections',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=True),
    sa.Column('scope', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('name', _name(191), nullable=False),
    sa.Column('config', sa.JSON(), nullable=True),
    sa.Column('sealed', sa.Text(), nullable=True),
    sa.Column('key_id', sa.String(length=8), nullable=True),
    sa.Column('identity', sa.JSON(), nullable=True),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    _ts('verified_at', True),
    _ts('created_at', False),
    _ts('updated_at', False),
    sa.Column('updated_by', sa.String(length=36), nullable=True),
    sa.CheckConstraint(_in('kind', KINDS), name=op.f('ck_connections_kind')),
    sa.CheckConstraint(_in('state', STATES), name=op.f('ck_connections_state')),
    sa.CheckConstraint(SCOPE_MATCHES_DEFINITION, name=op.f('ck_connections_scope')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_connections_team_id_teams')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_connections_project_id_projects')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_connections')),
    sa.UniqueConstraint('scope', 'name', name=op.f('uq_connections_scope_name'))
    )
    op.create_index(op.f('ix_connections_team_id'), 'connections', ['team_id'], unique=False)
    op.create_table('github_installations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('connection_id', sa.String(length=36), nullable=False),
    sa.Column('owner', sa.String(length=100), nullable=False),
    sa.Column('installation_id', sa.BigInteger(), nullable=False),
    sa.Column('permissions', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['connection_id'], ['connections.id'],
                            name=op.f('fk_github_installations_connection_id_connections')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_github_installations')),
    sa.UniqueConstraint('connection_id', 'owner', name=op.f('uq_github_installations_connection_id_owner'))
    )
    with op.batch_alter_table('webhooks') as batch:
        batch.add_column(sa.Column('connection_id', sa.String(length=36), nullable=True))
        batch.create_foreign_key(op.f('fk_webhooks_connection_id_connections'), 'connections', ['connection_id'],
                                 ['id'])
        batch.alter_column('secret_name', existing_type=sa.String(length=128), nullable=True)
        batch.create_check_constraint(op.f('ck_webhooks_secret'), WEBHOOK_SECRET)
    _sqlite_foreign_keys(True)


def downgrade() -> None:
    """Drop connections. A webhook that verified with one gets a placeholder secret name and is disabled."""
    _sqlite_foreign_keys(False)
    op.execute("UPDATE webhooks SET secret_name = 'WEBHOOK_SECRET', enabled = false WHERE secret_name IS NULL")
    with op.batch_alter_table('webhooks') as batch:
        batch.drop_constraint(op.f('ck_webhooks_secret'), type_='check')
        batch.alter_column('secret_name', existing_type=sa.String(length=128), nullable=False)
        batch.drop_constraint(op.f('fk_webhooks_connection_id_connections'), type_='foreignkey')
        batch.drop_column('connection_id')
    op.drop_table('github_installations')
    op.drop_index(op.f('ix_connections_team_id'), table_name='connections')
    op.drop_table('connections')
    _sqlite_foreign_keys(True)
