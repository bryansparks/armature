"""Webhook approval: Jira and Linear sources, held deliveries, and the senders a webhook is pinned to.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-27 22:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0012'
down_revision: str | Sequence[str] | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The values armature.server.db.schema allowed at this revision, frozen here.
OLD_SOURCES = ('github', 'slack', 'generic')
NEW_SOURCES = ('jira', 'linear')
SOURCES = ('github', 'slack', *NEW_SOURCES, 'generic')
OLD_STATUSES = ('accepted', 'ignored', 'duplicate', 'rejected', 'error')
STATUSES = (*OLD_STATUSES, 'held')
APPROVALS = ('pending', 'approved')


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


# SQLite rebuilds webhooks to change its checks, and dropping the old table would trip the deliveries and triggers
# that reference it.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def upgrade() -> None:
    _sqlite_foreign_keys(False)
    # Webhooks that already exist were trusted before approval existed, so they start approved with pinning off.
    with op.batch_alter_table('webhooks') as batch:
        batch.add_column(sa.Column('approval', sa.String(length=16), nullable=False, server_default='approved'))
        batch.add_column(sa.Column('pinned_senders', sa.JSON(), nullable=True))
        batch.drop_constraint(op.f('ck_webhooks_source'), type_='check')
        batch.create_check_constraint(op.f('ck_webhooks_source'), _in('source', SOURCES))
        batch.create_check_constraint(op.f('ck_webhooks_approval'), _in('approval', APPROVALS))
    # Once the existing rows are filled in, a row that leaves approval out starts pending.
    with op.batch_alter_table('webhooks') as batch:
        batch.alter_column('approval', existing_type=sa.String(length=16), existing_nullable=False,
                           server_default='pending')
    with op.batch_alter_table('webhook_deliveries') as batch:
        batch.add_column(sa.Column('sender', sa.String(length=255), nullable=True))
        batch.drop_constraint(op.f('ck_webhook_deliveries_status'), type_='check')
        batch.create_check_constraint(op.f('ck_webhook_deliveries_status'), _in('status', STATUSES))
    _sqlite_foreign_keys(True)


def downgrade() -> None:
    _sqlite_foreign_keys(False)
    # Without approval a pending webhook would start queuing jobs, so it goes down disabled.
    op.execute("UPDATE webhooks SET enabled = false WHERE approval = 'pending'")
    op.execute("UPDATE webhook_deliveries SET status = 'ignored' WHERE status = 'held'")
    new = f"(SELECT id FROM webhooks WHERE {_in('source', NEW_SOURCES)})"
    rules = f"(SELECT id FROM triggers WHERE webhook_id IN {new})"
    op.execute(f"UPDATE jobs SET trigger_id = NULL WHERE trigger_id IN {rules}")
    op.execute(f"DELETE FROM triggers WHERE webhook_id IN {new}")
    op.execute(f"DELETE FROM webhook_deliveries WHERE webhook_id IN {new}")
    op.execute(f"DELETE FROM webhooks WHERE {_in('source', NEW_SOURCES)}")
    with op.batch_alter_table('webhook_deliveries') as batch:
        batch.drop_constraint(op.f('ck_webhook_deliveries_status'), type_='check')
        batch.create_check_constraint(op.f('ck_webhook_deliveries_status'), _in('status', OLD_STATUSES))
        batch.drop_column('sender')
    with op.batch_alter_table('webhooks') as batch:
        batch.drop_constraint(op.f('ck_webhooks_approval'), type_='check')
        batch.drop_constraint(op.f('ck_webhooks_source'), type_='check')
        batch.create_check_constraint(op.f('ck_webhooks_source'), _in('source', OLD_SOURCES))
        batch.drop_column('pinned_senders')
        batch.drop_column('approval')
    _sqlite_foreign_keys(True)
