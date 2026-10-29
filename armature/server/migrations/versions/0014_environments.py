"""AWS environments: the connection, environment types, Mac hosts, environments, and a project's environment type.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-29 09:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '0014'
down_revision: str | Sequence[str] | None = '0013'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The values armature.server.db.schema allowed at this revision, frozen here.
AUTH_KINDS = ('access_key', 'assume_role', 'access_key_assume_role')
AWS_STATES = ('unverified', 'ok', 'error')
KINDS = ('ec2-linux', 'ec2-mac')
MARKETS = ('spot', 'spot-then-on-demand', 'on-demand')
HOST_STATES = ('allocating', 'running', 'releasing', 'released')
PURPOSES = ('job', 'warm')
STATES = ('requested', 'booting', 'ready', 'running', 'terminating', 'terminated', 'failed')
LIVE = "('requested', 'booting', 'ready', 'running', 'terminating')"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ts(name: str, nullable: bool) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=nullable)


def _name(length: int) -> sa.String:
    return sa.String(length=length).with_variant(mysql.VARCHAR(length=length, collation='utf8mb4_bin'), 'mysql')


# As in 0013: SQLite rebuilds projects, and other tables reference it.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def upgrade() -> None:
    _sqlite_foreign_keys(False)
    op.create_table('aws_connections',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', _name(191), nullable=False),
    sa.Column('region', sa.String(length=32), nullable=False),
    sa.Column('auth_kind', sa.String(length=32), nullable=False),
    sa.Column('sealed', sa.Text(), nullable=True),
    sa.Column('key_id', sa.String(length=8), nullable=True),
    sa.Column('vpc_id', sa.String(length=64), nullable=True),
    sa.Column('subnets', sa.JSON(), nullable=True),
    sa.Column('security_group_id', sa.String(length=64), nullable=True),
    sa.Column('instance_profile', sa.String(length=191), nullable=True),
    sa.Column('launch_template_ids', sa.JSON(), nullable=True),
    sa.Column('mac_instance_profile', sa.String(length=191), nullable=True),
    sa.Column('image_repository_uri', sa.String(length=255), nullable=True),
    sa.Column('allowed_instance_types', sa.JSON(), nullable=True),
    sa.Column('stack_name', sa.String(length=128), nullable=True),
    sa.Column('session_role_arn', sa.String(length=255), nullable=True),
    sa.Column('artifact_bucket', sa.String(length=255), nullable=True),
    sa.Column('tag_project', sa.String(length=64), nullable=False),
    sa.Column('extra_tags', sa.JSON(), nullable=True),
    sa.Column('install_id', sa.String(length=64), nullable=False),
    sa.Column('allow_host_allocation', sa.Boolean(), nullable=False),
    sa.Column('max_concurrent', sa.Integer(), nullable=False),
    sa.Column('max_daily_launches', sa.Integer(), nullable=False),
    sa.Column('daily_budget_usd', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('boot_timeout_s', sa.Integer(), nullable=False),
    sa.Column('max_lifetime_s', sa.Integer(), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    _ts('verified_at', True),
    _ts('created_at', False),
    _ts('updated_at', False),
    sa.Column('updated_by', sa.String(length=36), nullable=True),
    sa.CheckConstraint(_in('auth_kind', AUTH_KINDS), name=op.f('ck_aws_connections_auth_kind')),
    sa.CheckConstraint(_in('state', AWS_STATES), name=op.f('ck_aws_connections_state')),
    sa.CheckConstraint("tag_project <> 'armature'", name=op.f('ck_aws_connections_tag_project')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_aws_connections')),
    sa.UniqueConstraint('name', name=op.f('uq_aws_connections_name'))
    )
    op.create_table('environment_types',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', _name(191), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('image', sa.String(length=191), nullable=False),
    sa.Column('base_image', sa.String(length=191), nullable=True),
    sa.Column('installs', sa.JSON(), nullable=True),
    sa.Column('instance_types', sa.JSON(), nullable=True),
    sa.Column('market', sa.String(length=32), nullable=False),
    sa.Column('disk_gb', sa.Integer(), nullable=True),
    sa.Column('vm_cpu', sa.Integer(), nullable=True),
    sa.Column('vm_memory_gb', sa.Integer(), nullable=True),
    sa.Column('capabilities', sa.JSON(), nullable=True),
    sa.Column('boot_timeout_s', sa.Integer(), nullable=True),
    sa.Column('job_timeout_s', sa.Integer(), nullable=False),
    sa.Column('hourly_cost_usd', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('health_check', sa.JSON(), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    _ts('created_at', False),
    _ts('updated_at', False),
    sa.Column('updated_by', sa.String(length=36), nullable=True),
    sa.CheckConstraint(_in('kind', KINDS), name=op.f('ck_environment_types_kind')),
    sa.CheckConstraint(_in('market', MARKETS), name=op.f('ck_environment_types_market')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_environment_types')),
    sa.UniqueConstraint('name', name=op.f('uq_environment_types_name'))
    )
    op.create_table('environment_hosts',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('connection_id', sa.String(length=36), nullable=False),
    sa.Column('name', _name(191), nullable=False),
    sa.Column('instance_id', sa.String(length=64), nullable=True),
    sa.Column('dedicated_host_id', sa.String(length=64), nullable=True),
    sa.Column('zone', sa.String(length=32), nullable=True),
    sa.Column('instance_type', sa.String(length=32), nullable=True),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('slots', sa.Integer(), nullable=False),
    _ts('allocated_at', False),
    _ts('min_release_at', False),
    _ts('idle_since', True),
    sa.Column('auto_release_after_s', sa.Integer(), nullable=True),
    sa.Column('created_by', sa.String(length=36), nullable=True),
    sa.CheckConstraint(_in('state', HOST_STATES), name=op.f('ck_environment_hosts_state')),
    sa.ForeignKeyConstraint(['connection_id'], ['aws_connections.id'],
                            name=op.f('fk_environment_hosts_connection_id_aws_connections')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_environment_hosts')),
    sa.UniqueConstraint('name', name=op.f('uq_environment_hosts_name'))
    )
    op.create_table('environments',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('connection_id', sa.String(length=36), nullable=False),
    sa.Column('type_id', sa.String(length=36), nullable=True),
    sa.Column('purpose', sa.String(length=8), nullable=False),
    sa.Column('job_id', sa.String(length=36), nullable=True),
    sa.Column('runner_key_id', sa.String(length=36), nullable=True),
    sa.Column('team_id', sa.String(length=36), nullable=True),
    sa.Column('project_id', sa.String(length=36), nullable=True),
    sa.Column('host_id', sa.String(length=36), nullable=True),
    sa.Column('slot_index', sa.Integer(), nullable=True),
    sa.Column('provider_id', sa.String(length=64), nullable=True),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('market', sa.String(length=32), nullable=True),
    sa.Column('instance_type', sa.String(length=32), nullable=True),
    sa.Column('subnet_id', sa.String(length=64), nullable=True),
    sa.Column('launch_attempts', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('enroll_hmac', sa.String(length=64), nullable=True),
    _ts('enroll_expires_at', True),
    _ts('enrolled_at', True),
    _ts('requested_at', False),
    _ts('launched_at', True),
    _ts('ready_at', True),
    _ts('terminate_after', True),
    _ts('terminated_at', True),
    sa.CheckConstraint(_in('purpose', PURPOSES), name=op.f('ck_environments_purpose')),
    sa.CheckConstraint(_in('state', STATES), name=op.f('ck_environments_state')),
    sa.ForeignKeyConstraint(['connection_id'], ['aws_connections.id'],
                            name=op.f('fk_environments_connection_id_aws_connections')),
    sa.ForeignKeyConstraint(['type_id'], ['environment_types.id'],
                            name=op.f('fk_environments_type_id_environment_types')),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], name=op.f('fk_environments_job_id_jobs')),
    sa.ForeignKeyConstraint(['runner_key_id'], ['runner_keys.id'],
                            name=op.f('fk_environments_runner_key_id_runner_keys')),
    sa.ForeignKeyConstraint(['host_id'], ['environment_hosts.id'],
                            name=op.f('fk_environments_host_id_environment_hosts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_environments')),
    sa.UniqueConstraint('enroll_hmac', name=op.f('uq_environments_enroll_hmac')),
    sa.UniqueConstraint('host_id', 'slot_index', name=op.f('uq_environments_host_id_slot_index'))
    )
    op.create_index(op.f('ix_environments_job_id'), 'environments', ['job_id'], unique=False)
    op.create_index(op.f('ix_environments_state'), 'environments', ['state'], unique=False)
    with op.batch_alter_table('projects') as batch:
        batch.add_column(sa.Column('environment_type_id', sa.String(length=36), nullable=True))
        batch.create_foreign_key(op.f('fk_projects_environment_type_id_environment_types'), 'environment_types',
                                 ['environment_type_id'], ['id'])
    _sqlite_foreign_keys(True)


def downgrade() -> None:
    """Drop the environment tables. Instances still running in AWS are not terminated: do that in AWS first."""
    _sqlite_foreign_keys(False)
    # A job waiting on an environment has nothing left to start it, so it reads as failed, as in 0013.
    op.execute(f"UPDATE environments SET state = 'terminated', slot_index = NULL WHERE state IN {LIVE}")
    op.execute("UPDATE jobs SET status = 'failed' WHERE status = 'provisioning'")
    with op.batch_alter_table('projects') as batch:
        batch.drop_constraint(op.f('fk_projects_environment_type_id_environment_types'), type_='foreignkey')
        batch.drop_column('environment_type_id')
    op.drop_index(op.f('ix_environments_state'), table_name='environments')
    op.drop_index(op.f('ix_environments_job_id'), table_name='environments')
    op.drop_table('environments')
    op.drop_table('environment_hosts')
    op.drop_table('environment_types')
    op.drop_table('aws_connections')
    _sqlite_foreign_keys(True)
