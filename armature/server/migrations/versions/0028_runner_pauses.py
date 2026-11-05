"""Runners: whether a runner can pause a run at a gate, so one that can't never gets a gated job.

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-02 12:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0028'
down_revision: str | Sequence[str] | None = '0027'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('runners') as batch:
        batch.add_column(sa.Column('pauses', sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('runners') as batch:
        batch.drop_column('pauses')
