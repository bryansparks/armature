"""Run location: where each run executed, the platform each runner reports, and jobs by run.

Revision ID: 0024
Revises: 0019
Create Date: 2026-10-28 18:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0024'
down_revision: str | Sequence[str] | None = '0019'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# As in 0013: SQLite rebuilds runs and runners, and other tables reference them.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


# Existing runs stay NULL until `armature server backfill-locations` reads their events.
def upgrade() -> None:
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('runs') as batch:
        batch.add_column(sa.Column('location', sa.JSON(), nullable=True))
    with op.batch_alter_table('runners') as batch:
        batch.add_column(sa.Column('platform', sa.JSON(), nullable=True))
    op.create_index(op.f('ix_jobs_run_id'), 'jobs', ['run_id'])
    _sqlite_foreign_keys(True)


def downgrade() -> None:
    _sqlite_foreign_keys(False)
    op.drop_index(op.f('ix_jobs_run_id'), table_name='jobs')
    with op.batch_alter_table('runners') as batch:
        batch.drop_column('platform')
    with op.batch_alter_table('runs') as batch:
        batch.drop_column('location')
    _sqlite_foreign_keys(True)
