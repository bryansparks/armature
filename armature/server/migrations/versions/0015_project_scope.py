"""Project scope: providers and settings can belong to a project, and jobs pin the workflows they call.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-30 09:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0015'
down_revision: str | Sequence[str] | None = '0014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCOPE_MATCHES_TEAM = "(team_id IS NULL AND scope = '*') OR (team_id IS NOT NULL AND scope = team_id)"
SCOPE_MATCHES_DEFINITION = (f"(project_id IS NULL AND ({SCOPE_MATCHES_TEAM})) "
                            "OR (project_id IS NOT NULL AND team_id IS NOT NULL AND scope = project_id)")


# As in 0004: SQLite rebuilds these tables, and other tables reference them.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def upgrade() -> None:
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('providers') as batch:
        batch.add_column(sa.Column('project_id', sa.String(length=36), nullable=True))
        batch.add_column(sa.Column('scope', sa.String(length=36), nullable=True))
        batch.create_foreign_key(op.f('fk_providers_project_id_projects'), 'projects', ['project_id'], ['id'])
    op.execute("UPDATE providers SET scope = team_id")
    with op.batch_alter_table('providers') as batch:
        batch.alter_column('scope', existing_type=sa.String(length=36), nullable=False)
        batch.create_check_constraint(op.f('ck_providers_scope'), SCOPE_MATCHES_DEFINITION)
        batch.create_index(op.f('ix_providers_scope'), ['scope'], unique=False)
    with op.batch_alter_table('settings') as batch:
        batch.add_column(sa.Column('project_id', sa.String(length=36), nullable=True))
        batch.create_foreign_key(op.f('fk_settings_project_id_projects'), 'projects', ['project_id'], ['id'])
        batch.drop_constraint(op.f('ck_settings_scope'), type_='check')
        batch.create_check_constraint(op.f('ck_settings_scope'), SCOPE_MATCHES_DEFINITION)
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('call_pins', sa.JSON(), nullable=True))
    _sqlite_foreign_keys(True)


def downgrade() -> None:
    """Drop project providers and project settings. Jobs lose their pins and resolve called workflows at claim."""
    _sqlite_foreign_keys(False)
    op.execute("DELETE FROM providers WHERE project_id IS NOT NULL")
    op.execute("DELETE FROM settings WHERE project_id IS NOT NULL")
    with op.batch_alter_table('jobs') as batch:
        batch.drop_column('call_pins')
    with op.batch_alter_table('settings') as batch:
        batch.drop_constraint(op.f('ck_settings_scope'), type_='check')
        batch.create_check_constraint(op.f('ck_settings_scope'), SCOPE_MATCHES_TEAM)
        batch.drop_constraint(op.f('fk_settings_project_id_projects'), type_='foreignkey')
        batch.drop_column('project_id')
    with op.batch_alter_table('providers') as batch:
        batch.drop_index(op.f('ix_providers_scope'))
        batch.drop_constraint(op.f('ck_providers_scope'), type_='check')
        batch.drop_constraint(op.f('fk_providers_project_id_projects'), type_='foreignkey')
        batch.drop_column('scope')
        batch.drop_column('project_id')
    _sqlite_foreign_keys(True)
