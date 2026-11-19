"""Review rounds: each run's round, previous round, and kind, and each finding's blocking flag and lineage.

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-16 18:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0035'
down_revision: str | Sequence[str] | None = '0034'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REVIEW_KINDS = ('full', 'incremental', 'discuss', 'resolve_declined')


# As in 0013: SQLite rebuilds runs and findings, and other tables reference them.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


# Existing rows stay NULL until `armature server backfill-rounds` derives them.
def upgrade() -> None:
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('runs') as batch:
        batch.add_column(sa.Column('review_round', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('prev_round_run_id', sa.String(length=36), nullable=True))
        batch.add_column(sa.Column('review_kind', sa.String(length=16), nullable=True))
        batch.create_check_constraint(op.f('ck_runs_review_kind'),
                                      f"review_kind IS NULL OR {_in('review_kind', REVIEW_KINDS)}")
    with op.batch_alter_table('findings') as batch:
        batch.add_column(sa.Column('blocking', sa.Boolean(), nullable=True))
        batch.add_column(sa.Column('lineage_key', sa.String(length=64), nullable=True))
        batch.create_index(op.f('ix_findings_project_id_lineage_key'), ['project_id', 'lineage_key'])
    _sqlite_foreign_keys(True)


def downgrade() -> None:
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('findings') as batch:
        batch.drop_index(op.f('ix_findings_project_id_lineage_key'))
        batch.drop_column('lineage_key')
        batch.drop_column('blocking')
    with op.batch_alter_table('runs') as batch:
        batch.drop_constraint(op.f('ck_runs_review_kind'), type_='check')
        batch.drop_column('review_kind')
        batch.drop_column('prev_round_run_id')
        batch.drop_column('review_round')
    _sqlite_foreign_keys(True)
