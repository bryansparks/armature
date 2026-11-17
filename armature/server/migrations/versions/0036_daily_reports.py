"""The daily report: one instance-wide row per local day.

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-17 10:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '0036'
down_revision: str | Sequence[str] | None = '0035'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('daily_reports',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('data', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_daily_reports')),
    sa.UniqueConstraint('day', name=op.f('uq_daily_reports_day'))
    )


def downgrade() -> None:
    op.drop_table('daily_reports')
