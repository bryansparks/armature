"""Conversations: a Slack or web chat thread, its messages, pending Slack account links, and gate card outboxes.

Revision ID: 0023
Revises: 0024
Create Date: 2026-11-04 09:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '0023'
down_revision: str | Sequence[str] | None = '0024'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The values armature.server.db.schema allowed at this revision, frozen here.
KINDS = ('slack', 'web')
STATES = ('open', 'closed')
AWAITING = ('user', 'agent', 'none')
ROLES = ('user', 'assistant', 'system')
CARD_STATES = ('post', 'posted', 'update', 'done', 'failed')


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ts(name: str, nullable: bool) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=nullable)


# As in 0019: SQLite rebuilds workflows and gates for their new columns, and other tables reference them.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def upgrade() -> None:
    _sqlite_foreign_keys(False)
    op.create_table('conversations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=True),
    sa.Column('workflow_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('thread', sa.String(length=191), nullable=False),
    sa.Column('external', sa.JSON(), nullable=True),
    sa.Column('owner_id', sa.String(length=36), nullable=True),
    sa.Column('state', sa.String(length=8), nullable=False),
    sa.Column('awaiting', sa.String(length=8), nullable=False),
    sa.Column('data', sa.JSON(), nullable=True),
    sa.Column('active_job_id', sa.String(length=36), nullable=True),
    sa.Column('turns', sa.Integer(), nullable=False),
    _ts('created_at', False),
    _ts('updated_at', False),
    _ts('closed_at', True),
    sa.CheckConstraint(_in('kind', KINDS), name=op.f('ck_conversations_kind')),
    sa.CheckConstraint(_in('state', STATES), name=op.f('ck_conversations_state')),
    sa.CheckConstraint(_in('awaiting', AWAITING), name=op.f('ck_conversations_awaiting')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_conversations_team_id_teams')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_conversations_project_id_projects')),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], name=op.f('fk_conversations_workflow_id_workflows')),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_conversations_owner_id_users')),
    sa.ForeignKeyConstraint(['active_job_id'], ['jobs.id'], name=op.f('fk_conversations_active_job_id_jobs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_conversations')),
    sa.UniqueConstraint('team_id', 'thread', name=op.f('uq_conversations_team_id_thread'))
    )
    op.create_index(op.f('ix_conversations_active_job_id'), 'conversations', ['active_job_id'], unique=False)
    op.create_table('conversation_messages',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('conversation_id', sa.String(length=36), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('author', sa.String(length=191), nullable=True),
    sa.Column('ext_ts', sa.String(length=32), nullable=True),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('handled', sa.Boolean(), nullable=False),
    _ts('created_at', False),
    _ts('posted_at', True),
    _ts('post_due_at', True),
    sa.Column('post_attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('post_error', sa.Text(), nullable=True),
    sa.CheckConstraint(_in('role', ROLES), name=op.f('ck_conversation_messages_role')),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'],
                            name=op.f('fk_conversation_messages_conversation_id_conversations')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_conversation_messages')),
    sa.UniqueConstraint('conversation_id', 'seq', name=op.f('uq_conversation_messages_conversation_id_seq')),
    sa.UniqueConstraint('conversation_id', 'ext_ts', name=op.f('uq_conversation_messages_conversation_id_ext_ts'))
    )
    op.create_index(op.f('ix_conversation_messages_conversation_id'), 'conversation_messages', ['conversation_id'],
                    unique=False)
    op.create_table('slack_links',
    sa.Column('user_id', sa.String(length=36), nullable=False),
    sa.Column('workspace_id', sa.String(length=32), nullable=False),
    sa.Column('slack_user_id', sa.String(length=32), nullable=False),
    sa.Column('code_hmac', sa.String(length=64), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    _ts('created_at', False),
    _ts('expires_at', False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_slack_links_user_id_users')),
    sa.PrimaryKeyConstraint('user_id', name=op.f('pk_slack_links'))
    )
    op.create_table('conversation_pending',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('thread', sa.String(length=191), nullable=False),
    sa.Column('ext_ts', sa.String(length=32), nullable=False),
    sa.Column('author', sa.String(length=191), nullable=True),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('delivery_id', sa.String(length=36), nullable=True),
    _ts('created_at', False),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_conversation_pending_team_id_teams')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_conversation_pending')),
    sa.UniqueConstraint('team_id', 'thread', 'ext_ts', name=op.f('uq_conversation_pending_team_id_thread_ext_ts'))
    )
    op.create_index(op.f('ix_conversation_pending_created_at'), 'conversation_pending', ['created_at'], unique=False)
    with op.batch_alter_table('workflows') as batch:
        batch.add_column(sa.Column('built_in', sa.Boolean(), server_default=sa.false(), nullable=False))
    with op.batch_alter_table('gates') as batch:
        batch.add_column(sa.Column('card_state', sa.String(length=8), nullable=True))
        batch.add_column(_ts('card_due_at', True))
        batch.add_column(sa.Column('card_attempts', sa.Integer(), server_default='0', nullable=False))
        batch.add_column(sa.Column('card_error', sa.Text(), nullable=True))
        batch.create_check_constraint(op.f('ck_gates_card_state'),
                                      f"card_state IS NULL OR {_in('card_state', CARD_STATES)}")
    _sqlite_foreign_keys(True)


def downgrade() -> None:
    """Drop conversations. A queued turn has no conversation left to read, so it fails."""
    _sqlite_foreign_keys(False)
    op.execute("UPDATE jobs SET status = 'failed', error = 'The server was downgraded before this turn ran.' "
               "WHERE status = 'queued' AND id IN (SELECT active_job_id FROM conversations)")
    with op.batch_alter_table('gates') as batch:
        batch.drop_constraint(op.f('ck_gates_card_state'), type_='check')
        batch.drop_column('card_error')
        batch.drop_column('card_attempts')
        batch.drop_column('card_due_at')
        batch.drop_column('card_state')
    with op.batch_alter_table('workflows') as batch:
        batch.drop_column('built_in')
    op.drop_index(op.f('ix_conversation_pending_created_at'), table_name='conversation_pending')
    op.drop_table('conversation_pending')
    op.drop_table('slack_links')
    op.drop_index(op.f('ix_conversation_messages_conversation_id'), table_name='conversation_messages')
    op.drop_table('conversation_messages')
    op.drop_index(op.f('ix_conversations_active_job_id'), table_name='conversations')
    op.drop_table('conversations')
    _sqlite_foreign_keys(True)
