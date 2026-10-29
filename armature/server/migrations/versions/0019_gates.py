"""Gates: human approvals a run pauses at, the thread a job carries, and a user's Slack account.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-02 09:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '0019'
down_revision: str | Sequence[str] | None = '0018'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The values armature.server.db.schema allowed at this revision, frozen here.
STATES = ('open', 'approved', 'rejected', 'expired', 'canceled')
ROLES = ('admin', 'team_lead', 'project_lead', 'contributor', 'viewer')
VIA = ('web', 'slack')


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ts(name: str, nullable: bool) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=nullable)


# As in 0013: SQLite rebuilds users for the unique constraint, and other tables reference it.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def upgrade() -> None:
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('thread', sa.String(length=191), nullable=True))
        batch.add_column(sa.Column('resume_of_gate', sa.String(length=36), nullable=True))
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('slack_workspace_id', sa.String(length=32), nullable=True))
        batch.add_column(sa.Column('slack_user_id', sa.String(length=32), nullable=True))
        batch.create_unique_constraint(op.f('uq_users_slack_workspace_id_slack_user_id'),
                                       ['slack_workspace_id', 'slack_user_id'])
    op.create_table('gates',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('job_id', sa.String(length=36), nullable=True),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=True),
    sa.Column('step_id', sa.String(length=191), nullable=False),
    sa.Column('step_key', sa.String(length=191), nullable=False),
    sa.Column('present', sa.Text(), nullable=True),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('min_role', sa.String(length=16), nullable=False),
    sa.Column('approvers', sa.JSON(), nullable=True),
    sa.Column('nonce', sa.String(length=64), nullable=False),
    sa.Column('checkpoint', sa.Text().with_variant(mysql.MEDIUMTEXT(), 'mysql'), nullable=True),
    sa.Column('workflow_version', sa.Integer(), nullable=True),
    sa.Column('thread', sa.String(length=191), nullable=True),
    sa.Column('channel', sa.JSON(), nullable=True),
    _ts('created_at', False),
    _ts('expires_at', False),
    _ts('decided_at', True),
    sa.Column('decided_by', sa.String(length=36), nullable=True),
    sa.Column('decided_via', sa.String(length=8), nullable=True),
    sa.Column('decision_note', sa.Text(), nullable=True),
    sa.Column('resume_job_id', sa.String(length=36), nullable=True),
    sa.CheckConstraint(_in('state', STATES), name=op.f('ck_gates_state')),
    sa.CheckConstraint(_in('min_role', ROLES), name=op.f('ck_gates_min_role')),
    sa.CheckConstraint(_in('decided_via', VIA), name=op.f('ck_gates_decided_via')),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_gates_run_id_runs')),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], name=op.f('fk_gates_job_id_jobs')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_gates_team_id_teams')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_gates_project_id_projects')),
    sa.ForeignKeyConstraint(['resume_job_id'], ['jobs.id'], name=op.f('fk_gates_resume_job_id_jobs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_gates')),
    sa.UniqueConstraint('run_id', 'step_key', name=op.f('uq_gates_run_id_step_key'))
    )
    op.create_index(op.f('ix_gates_run_id'), 'gates', ['run_id'], unique=False)
    op.create_index(op.f('ix_gates_state_expires_at'), 'gates', ['state', 'expires_at'], unique=False)
    _sqlite_foreign_keys(True)


def downgrade() -> None:
    """Drop gates. A run still waiting at one, or a job queued to resume one, has nothing left to finish it."""
    _sqlite_foreign_keys(False)
    op.execute("UPDATE runs SET status = 'failed', error = 'The server was downgraded while this run waited at "
               "a gate.' WHERE status = 'waiting'")
    op.execute("UPDATE jobs SET status = 'failed' WHERE status = 'suspended' OR "
               "(status = 'queued' AND resume_of_gate IS NOT NULL)")
    op.drop_index(op.f('ix_gates_state_expires_at'), table_name='gates')
    op.drop_index(op.f('ix_gates_run_id'), table_name='gates')
    op.drop_table('gates')
    with op.batch_alter_table('users') as batch:
        batch.drop_constraint(op.f('uq_users_slack_workspace_id_slack_user_id'), type_='unique')
        batch.drop_column('slack_user_id')
        batch.drop_column('slack_workspace_id')
    with op.batch_alter_table('jobs') as batch:
        batch.drop_column('resume_of_gate')
        batch.drop_column('thread')
    _sqlite_foreign_keys(True)
