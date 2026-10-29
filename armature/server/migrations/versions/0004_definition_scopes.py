"""Definition scopes: workflows and documents can belong to a project.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-23 21:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0004'
down_revision: str | Sequence[str] | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCOPE_MATCHES_TEAM = "(team_id IS NULL AND scope = '*') OR (team_id IS NOT NULL AND scope = team_id)"
SCOPE_MATCHES_DEFINITION = (f"(project_id IS NULL AND ({SCOPE_MATCHES_TEAM})) "
                            "OR (project_id IS NOT NULL AND team_id IS NOT NULL AND scope = project_id)")
TABLES = ('workflows', 'documents')


# As in 0003: SQLite rebuilds these tables, and runs, jobs, and triggers reference workflows.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def upgrade() -> None:
    _sqlite_foreign_keys(False)
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column('project_id', sa.String(length=36), nullable=True))
            batch.create_foreign_key(op.f(f'fk_{table}_project_id_projects'), 'projects', ['project_id'], ['id'])
            batch.drop_constraint(op.f(f'ck_{table}_scope'), type_='check')
            batch.create_check_constraint(op.f(f'ck_{table}_scope'), SCOPE_MATCHES_DEFINITION)
    _sqlite_foreign_keys(True)


def downgrade() -> None:
    _sqlite_foreign_keys(False)
    scoped = "(SELECT id FROM workflows WHERE project_id IS NOT NULL)"
    op.execute(f"UPDATE runs SET workflow_id = NULL WHERE workflow_id IN {scoped}")
    for table in ("jobs", "triggers"):
        op.execute(f"DELETE FROM {table} WHERE workflow_id IN {scoped}")
    op.execute(f"DELETE FROM versions WHERE object_kind = 'workflow' AND object_id IN {scoped}")
    op.execute("DELETE FROM versions WHERE object_kind = 'document' AND object_id IN "
               "(SELECT id FROM documents WHERE project_id IS NOT NULL)")
    for table in TABLES:
        op.execute(f"DELETE FROM {table} WHERE project_id IS NOT NULL")
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(op.f(f'ck_{table}_scope'), type_='check')
            batch.create_check_constraint(op.f(f'ck_{table}_scope'), SCOPE_MATCHES_TEAM)
            batch.drop_constraint(op.f(f'fk_{table}_project_id_projects'), type_='foreignkey')
            batch.drop_column('project_id')
    _sqlite_foreign_keys(True)
