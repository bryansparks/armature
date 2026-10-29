"""Webhooks: per-webhook URLs, their deliveries, and the rules that queue jobs from them.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-27 18:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '0010'
down_revision: str | Sequence[str] | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name: str, nullable: bool) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=nullable)


def upgrade() -> None:
    op.create_table('webhooks',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=191).with_variant(mysql.VARCHAR(length=191, collation='utf8mb4_bin'), 'mysql'),
              nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('path_token', sa.String(length=64), nullable=False),
    sa.Column('secret_name', sa.String(length=128), nullable=False),
    sa.Column('generic_auth', sa.String(length=16), nullable=True),
    sa.Column('generic_signature_header', sa.String(length=64), nullable=True),
    sa.Column('generic_delivery_header', sa.String(length=64), nullable=True),
    sa.Column('forward_url', sa.Text(), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    _ts('created_at', False),
    _ts('updated_at', False),
    sa.CheckConstraint("source IN ('github', 'slack', 'generic')", name=op.f('ck_webhooks_source')),
    sa.CheckConstraint("generic_auth IS NULL OR generic_auth IN ('hmac', 'bearer')",
                       name=op.f('ck_webhooks_generic_auth')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_webhooks_team_id_teams')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_webhooks')),
    sa.UniqueConstraint('path_token', name=op.f('uq_webhooks_path_token')),
    sa.UniqueConstraint('team_id', 'name', name=op.f('uq_webhooks_team_id_name'))
    )
    op.create_index(op.f('ix_webhooks_team_id'), 'webhooks', ['team_id'])
    op.create_table('webhook_deliveries',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('webhook_id', sa.String(length=36), nullable=False),
    _ts('received_at', False),
    sa.Column('delivery_key', sa.String(length=191), nullable=True),
    sa.Column('event', sa.String(length=64), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('payload', sa.Text().with_variant(mysql.MEDIUMTEXT(), 'mysql'), nullable=True),
    sa.Column('headers', sa.JSON(), nullable=True),
    sa.Column('matched', sa.JSON(), nullable=True),
    sa.Column('forward_status', sa.String(length=32), nullable=True),
    sa.CheckConstraint("status IN ('accepted', 'ignored', 'duplicate', 'rejected', 'error')",
                       name=op.f('ck_webhook_deliveries_status')),
    sa.ForeignKeyConstraint(['webhook_id'], ['webhooks.id'], name=op.f('fk_webhook_deliveries_webhook_id_webhooks')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_webhook_deliveries')),
    sa.UniqueConstraint('webhook_id', 'delivery_key', name=op.f('uq_webhook_deliveries_webhook_id_delivery_key'))
    )
    op.create_index(op.f('ix_webhook_deliveries_webhook_id_received_at'), 'webhook_deliveries',
                    ['webhook_id', 'received_at'])
    op.create_index(op.f('ix_webhook_deliveries_received_at'), 'webhook_deliveries', ['received_at'])
    with op.batch_alter_table('triggers') as batch:
        batch.add_column(sa.Column('webhook_id', sa.String(length=36), nullable=True))
        batch.create_foreign_key(op.f('fk_triggers_webhook_id_webhooks'), 'webhooks', ['webhook_id'], ['id'])
        batch.create_index(op.f('ix_triggers_webhook_id'), ['webhook_id'])
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('trigger_id', sa.String(length=36), nullable=True))
        batch.add_column(sa.Column('delivery_id', sa.String(length=36), nullable=True))
        batch.add_column(sa.Column('workflow_version', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('error', sa.Text(), nullable=True))
        batch.add_column(_ts('finished_at', True))
        batch.create_foreign_key(op.f('fk_jobs_trigger_id_triggers'), 'triggers', ['trigger_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.drop_constraint(op.f('fk_jobs_trigger_id_triggers'), type_='foreignkey')
        batch.drop_column('finished_at')
        batch.drop_column('error')
        batch.drop_column('workflow_version')
        batch.drop_column('delivery_id')
        batch.drop_column('trigger_id')
    with op.batch_alter_table('triggers') as batch:
        batch.drop_constraint(op.f('fk_triggers_webhook_id_webhooks'), type_='foreignkey')
        batch.drop_index(op.f('ix_triggers_webhook_id'))
        batch.drop_column('webhook_id')
    op.drop_table('webhook_deliveries')
    op.drop_table('webhooks')
