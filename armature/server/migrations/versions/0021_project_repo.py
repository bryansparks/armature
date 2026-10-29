"""Projects: the connection their repository is reached through, and its default branch.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-28 18:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0021'
down_revision: str | Sequence[str] | None = '0020'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# As in 0016: SQLite rebuilds projects, and other tables reference it.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def upgrade() -> None:
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('projects') as batch:
        batch.add_column(sa.Column('connection_id', sa.String(length=36), nullable=True))
        batch.add_column(sa.Column('default_branch', sa.String(length=191), nullable=True))
        batch.create_foreign_key(op.f('fk_projects_connection_id_connections'), 'connections', ['connection_id'],
                                 ['id'])
    _sqlite_foreign_keys(True)


def downgrade() -> None:
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('projects') as batch:
        batch.drop_constraint(op.f('fk_projects_connection_id_connections'), type_='foreignkey')
        batch.drop_column('default_branch')
        batch.drop_column('connection_id')
    _sqlite_foreign_keys(True)
