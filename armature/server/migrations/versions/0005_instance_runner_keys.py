"""Instance runner keys: a runner key can belong to the instance instead of one team.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-23 23:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0005'
down_revision: str | Sequence[str] | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCOPE_MATCHES_TEAM = "(team_id IS NULL AND scope = '*') OR (team_id IS NOT NULL AND scope = team_id)"


def upgrade() -> None:
    op.add_column('runner_keys', sa.Column('scope', sa.String(length=36), nullable=True))
    op.execute("UPDATE runner_keys SET scope = team_id")
    with op.batch_alter_table('runner_keys') as batch:
        batch.alter_column('scope', existing_type=sa.String(length=36), nullable=False)
        batch.alter_column('team_id', existing_type=sa.String(length=36), nullable=True)
        batch.create_check_constraint(op.f('ck_runner_keys_scope'), SCOPE_MATCHES_TEAM)


def downgrade() -> None:
    instance_keys = "(SELECT id FROM runner_keys WHERE team_id IS NULL)"
    op.execute(f"DELETE FROM run_tokens WHERE run_id IN (SELECT id FROM runs WHERE runner_key_id IN {instance_keys})")
    op.execute("DELETE FROM runner_keys WHERE team_id IS NULL")
    with op.batch_alter_table('runner_keys') as batch:
        batch.drop_constraint(op.f('ck_runner_keys_scope'), type_='check')
        batch.alter_column('team_id', existing_type=sa.String(length=36), nullable=False)
        batch.drop_column('scope')
