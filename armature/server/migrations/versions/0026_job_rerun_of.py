"""Jobs: the run a job reruns, so the run it opens links back.

Revision ID: 0026
Revises: 0022
Create Date: 2026-10-29 12:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0026'
down_revision: str | Sequence[str] | None = '0022'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# As in 0019: SQLite rebuilds jobs, and gates reference it.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def upgrade() -> None:
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('rerun_of', sa.String(length=36), nullable=True))
        batch.create_index(batch.f('ix_jobs_rerun_of'), ['rerun_of'])
    _sqlite_foreign_keys(True)


def downgrade() -> None:
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('jobs') as batch:
        batch.drop_index(batch.f('ix_jobs_rerun_of'))
        batch.drop_column('rerun_of')
    _sqlite_foreign_keys(True)
