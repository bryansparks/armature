"""Run subject: what a live run is about, such as owner/repo#123 or an issue key.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-24 10:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0007'
down_revision: str | Sequence[str] | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('runs', sa.Column('subject', sa.String(length=191), nullable=True))
    op.create_index(op.f('ix_runs_subject_started_at'), 'runs', ['subject', 'started_at'])


def downgrade() -> None:
    op.drop_index(op.f('ix_runs_subject_started_at'), table_name='runs')
    with op.batch_alter_table('runs') as batch:
        batch.drop_column('subject')
