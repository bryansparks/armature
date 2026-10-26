"""Run artifacts: named reports a run writes, such as a code review's review.md.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-24 09:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '0006'
down_revision: str | Sequence[str] | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('run_artifacts',
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=100).with_variant(mysql.VARCHAR(100, collation="utf8mb4_bin"), "mysql"),
              nullable=False),
    sa.Column('step_id', sa.String(length=191), nullable=True),
    sa.Column('media_type', sa.String(length=64), nullable=False),
    sa.Column('content', sa.Text().with_variant(mysql.MEDIUMTEXT(), 'mysql'), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"),
              nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_run_artifacts_run_id_runs')),
    sa.PrimaryKeyConstraint('run_id', 'name', name=op.f('pk_run_artifacts'))
    )


def downgrade() -> None:
    op.drop_table('run_artifacts')
