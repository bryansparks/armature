"""Jobs: whether the rule that queued the job ran it dry, fixed at enqueue.

Revision ID: 0025
Revises: 0023
Create Date: 2026-10-28 20:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0025'
down_revision: str | Sequence[str] | None = '0023'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# As in 0019: SQLite rebuilds jobs, and gates reference it.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def upgrade() -> None:
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('dry_run', sa.Boolean(), nullable=False, server_default=sa.false()))
    _sqlite_foreign_keys(True)


def downgrade() -> None:
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('jobs') as batch:
        batch.drop_column('dry_run')
    _sqlite_foreign_keys(True)
