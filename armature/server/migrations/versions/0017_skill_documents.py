"""Skill documents: a Claude Code skill an agent: step loads, stored and versioned like a role.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-28 12:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0017'
down_revision: str | Sequence[str] | None = '0016'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BEFORE = ('role', 'prompt', 'mission')
AFTER = (*BEFORE, 'skill')

documents = sa.table('documents', sa.column('id', sa.String), sa.column('kind', sa.String))
versions = sa.table('versions', sa.column('object_kind', sa.String), sa.column('object_id', sa.String))


def _kinds(kinds: tuple[str, ...]) -> None:
    with op.batch_alter_table('documents') as batch:
        batch.drop_constraint(op.f('ck_documents_kind'), type_='check')
        batch.create_check_constraint(op.f('ck_documents_kind'), f"kind IN ({', '.join(repr(k) for k in kinds)})")


def upgrade() -> None:
    _kinds(AFTER)


def downgrade() -> None:
    conn = op.get_bind()
    skills = sa.select(documents.c.id).where(documents.c.kind == 'skill')
    conn.execute(sa.delete(versions).where(versions.c.object_kind == 'document', versions.c.object_id.in_(skills)))
    conn.execute(sa.delete(documents).where(documents.c.kind == 'skill'))
    _kinds(BEFORE)
