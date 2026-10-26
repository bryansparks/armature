"""Definitions: team slugs, project repositories, instance-level workflows, and scoped settings.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-23 18:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0003'
down_revision: str | Sequence[str] | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCOPE_MATCHES_TEAM = "(team_id IS NULL AND scope = '*') OR (team_id IS NOT NULL AND scope = team_id)"


# SQLite rebuilds workflows to change its keys, and dropping the old table would trip the runs, jobs, and triggers
# that reference it. The pragma only takes effect outside a transaction, which is where an upgrade of a live
# database starts. A fresh database has no rows to trip.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def upgrade() -> None:
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('teams') as batch:
        batch.add_column(sa.Column('slug', sa.String(length=40), nullable=True))
        batch.create_unique_constraint(op.f('uq_teams_slug'), ['slug'])
    op.add_column('projects', sa.Column('repo', sa.String(length=191), nullable=True))

    op.add_column('workflows', sa.Column('scope', sa.String(length=36), nullable=True))
    op.execute("UPDATE workflows SET scope = team_id")
    op.create_index(op.f('ix_workflows_team_id'), 'workflows', ['team_id'])
    with op.batch_alter_table('workflows') as batch:
        batch.alter_column('scope', existing_type=sa.String(length=36), nullable=False)
        batch.alter_column('team_id', existing_type=sa.String(length=36), nullable=True)
        batch.create_unique_constraint(op.f('uq_workflows_scope_name'), ['scope', 'name'])
        batch.drop_constraint(op.f('uq_workflows_team_id_name'), type_='unique')
        batch.create_check_constraint(op.f('ck_workflows_scope'), SCOPE_MATCHES_TEAM)

    with op.batch_alter_table('settings') as batch:
        batch.add_column(sa.Column('team_id', sa.String(length=36), nullable=True))
        batch.add_column(sa.Column('scope', sa.String(length=36), nullable=False, server_default='*'))
        batch.create_foreign_key(op.f('fk_settings_team_id_teams'), 'teams', ['team_id'], ['id'])
        batch.create_check_constraint(op.f('ck_settings_scope'), SCOPE_MATCHES_TEAM)
        batch.drop_constraint(op.f('pk_settings'), type_='primary')
        batch.create_primary_key(op.f('pk_settings'), ['scope', 'key'])
    _sqlite_foreign_keys(True)


def downgrade() -> None:
    _sqlite_foreign_keys(False)
    # Only instance settings fit the old primary key on key alone.
    op.execute("DELETE FROM settings WHERE scope <> '*'")
    with op.batch_alter_table('settings') as batch:
        batch.drop_constraint(op.f('pk_settings'), type_='primary')
        batch.create_primary_key(op.f('pk_settings'), ['key'])
        batch.drop_constraint(op.f('ck_settings_scope'), type_='check')
        batch.drop_constraint(op.f('fk_settings_team_id_teams'), type_='foreignkey')
        batch.drop_column('scope')
        batch.drop_column('team_id')

    # An instance workflow has no team to fall back to, so it goes with what hangs off it.
    instance = "(SELECT id FROM workflows WHERE team_id IS NULL)"
    op.execute(f"UPDATE runs SET workflow_id = NULL WHERE workflow_id IN {instance}")
    for table in ("jobs", "triggers"):
        op.execute(f"DELETE FROM {table} WHERE workflow_id IN {instance}")
    op.execute(f"DELETE FROM versions WHERE object_kind = 'workflow' AND object_id IN {instance}")
    op.execute("DELETE FROM workflows WHERE team_id IS NULL")
    with op.batch_alter_table('workflows') as batch:
        batch.drop_constraint(op.f('ck_workflows_scope'), type_='check')
        batch.create_unique_constraint(op.f('uq_workflows_team_id_name'), ['team_id', 'name'])
        batch.drop_constraint(op.f('uq_workflows_scope_name'), type_='unique')
        batch.alter_column('team_id', existing_type=sa.String(length=36), nullable=False)
        batch.drop_column('scope')
    op.drop_index(op.f('ix_workflows_team_id'), table_name='workflows')

    with op.batch_alter_table('projects') as batch:
        batch.drop_column('repo')
    with op.batch_alter_table('teams') as batch:
        batch.drop_constraint(op.f('uq_teams_slug'), type_='unique')
        batch.drop_column('slug')
    _sqlite_foreign_keys(True)
