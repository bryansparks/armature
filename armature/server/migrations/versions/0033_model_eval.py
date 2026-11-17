"""Model evaluation: experiments, their comparison runs, cross-run matches, judgments, scores, and recommendations,
a job's comparison run, and the arm mode of a tool call.

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-13 22:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = '0033'
down_revision: str | Sequence[str] | None = '0032'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MODES_BEFORE = ('live', 'dry')
MODES_AFTER = (*MODES_BEFORE, 'arm')

tool_calls = sa.table('tool_calls', sa.column('mode', sa.String))
llm_calls = sa.table('llm_calls', sa.column('run_id', sa.String))
finding_candidates = sa.table('finding_candidates', sa.column('arm_id', sa.String))
eval_arms = sa.table('eval_arms', sa.column('arm_run_id', sa.String))


def _ts(name: str, nullable: bool) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=nullable)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


# As in 0026: SQLite rebuilds jobs, and gates reference it.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def _modes(modes: tuple[str, ...]) -> None:
    with op.batch_alter_table('tool_calls') as batch:
        batch.drop_constraint(op.f('ck_tool_calls_mode'), type_='check')
        batch.create_check_constraint(op.f('ck_tool_calls_mode'), _in('mode', modes))


def upgrade() -> None:
    op.create_table('eval_experiments',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=True),
    sa.Column('specialist', sa.String(length=64), nullable=False),
    sa.Column('variable', sa.String(length=16), nullable=False),
    sa.Column('baseline', sa.JSON(), nullable=False),
    sa.Column('variant', sa.JSON(), nullable=False),
    sa.Column('sample_rate', sa.Float(), nullable=False),
    sa.Column('daily_budget_usd', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('max_concurrent_arms', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('min_prs', sa.Integer(), nullable=False),
    _ts('created_at', False),
    sa.Column('created_by', sa.String(length=36), nullable=True),
    sa.CheckConstraint(_in('variable', ('model', 'effort')), name=op.f('ck_eval_experiments_variable')),
    sa.CheckConstraint(_in('status', ('running', 'paused', 'finished')), name=op.f('ck_eval_experiments_status')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_eval_experiments_team_id_teams')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_eval_experiments_project_id_projects')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_eval_experiments'))
    )
    op.create_index(op.f('ix_eval_experiments_team_id_status'), 'eval_experiments', ['team_id', 'status'],
                    unique=False)
    op.create_table('eval_arms',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('experiment_id', sa.String(length=36), nullable=False),
    sa.Column('primary_run_id', sa.String(length=36), nullable=False),
    sa.Column('arm_run_id', sa.String(length=36), nullable=True),
    sa.Column('head_sha', sa.String(length=40), nullable=False),
    sa.Column('label', sa.String(length=16), nullable=False),
    sa.Column('overrides', sa.JSON(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('cost_usd', sa.Numeric(precision=14, scale=6), nullable=True),
    sa.Column('latency_s', sa.Float(), nullable=True),
    _ts('created_at', False),
    sa.CheckConstraint(_in('label', ('variant', 'control')), name=op.f('ck_eval_arms_label')),
    sa.CheckConstraint(_in('status', ('queued', 'running', 'done', 'failed', 'skipped')),
                       name=op.f('ck_eval_arms_status')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_eval_arms_team_id_teams')),
    sa.ForeignKeyConstraint(['experiment_id'], ['eval_experiments.id'],
                            name=op.f('fk_eval_arms_experiment_id_eval_experiments')),
    sa.ForeignKeyConstraint(['primary_run_id'], ['runs.id'], name=op.f('fk_eval_arms_primary_run_id_runs')),
    sa.ForeignKeyConstraint(['arm_run_id'], ['runs.id'], name=op.f('fk_eval_arms_arm_run_id_runs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_eval_arms')),
    sa.UniqueConstraint('experiment_id', 'primary_run_id', 'label',
                        name=op.f('uq_eval_arms_experiment_id_primary_run_id_label'))
    )
    op.create_index(op.f('ix_eval_arms_arm_run_id'), 'eval_arms', ['arm_run_id'], unique=False)
    op.create_table('eval_matches',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('experiment_id', sa.String(length=36), nullable=False),
    sa.Column('primary_run_id', sa.String(length=36), nullable=False),
    sa.Column('left_candidate_id', sa.String(length=36), nullable=False),
    sa.Column('right_candidate_id', sa.String(length=36), nullable=True),
    sa.Column('method', sa.String(length=16), nullable=False),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('matcher_version', sa.String(length=64), nullable=False),
    sa.CheckConstraint(_in('method', ('key', 'heuristic', 'none')), name=op.f('ck_eval_matches_method')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_eval_matches_team_id_teams')),
    sa.ForeignKeyConstraint(['experiment_id'], ['eval_experiments.id'],
                            name=op.f('fk_eval_matches_experiment_id_eval_experiments')),
    sa.ForeignKeyConstraint(['primary_run_id'], ['runs.id'], name=op.f('fk_eval_matches_primary_run_id_runs')),
    sa.ForeignKeyConstraint(['left_candidate_id'], ['finding_candidates.id'],
                            name=op.f('fk_eval_matches_left_candidate_id_finding_candidates')),
    sa.ForeignKeyConstraint(['right_candidate_id'], ['finding_candidates.id'],
                            name=op.f('fk_eval_matches_right_candidate_id_finding_candidates')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_eval_matches')),
    sa.UniqueConstraint('experiment_id', 'left_candidate_id', 'matcher_version',
                        name=op.f('uq_eval_matches_experiment_id_left_candidate_id_matcher_version'))
    )
    op.create_index(op.f('ix_eval_matches_experiment_id_primary_run_id'), 'eval_matches',
                    ['experiment_id', 'primary_run_id'], unique=False)
    op.create_table('eval_judgments',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('candidate_id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('experiment_id', sa.String(length=36), nullable=False),
    sa.Column('valid', sa.Boolean(), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('judge', sa.String(length=191), nullable=False),
    sa.Column('cost_usd', sa.Numeric(precision=14, scale=6), nullable=True),
    _ts('created_at', False),
    sa.ForeignKeyConstraint(['candidate_id'], ['finding_candidates.id'],
                            name=op.f('fk_eval_judgments_candidate_id_finding_candidates')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_eval_judgments_team_id_teams')),
    sa.ForeignKeyConstraint(['experiment_id'], ['eval_experiments.id'],
                            name=op.f('fk_eval_judgments_experiment_id_eval_experiments')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_eval_judgments')),
    sa.UniqueConstraint('experiment_id', 'candidate_id', name=op.f('uq_eval_judgments_experiment_id_candidate_id'))
    )
    op.create_index(op.f('ix_eval_judgments_experiment_id_created_at'), 'eval_judgments',
                    ['experiment_id', 'created_at'], unique=False)
    op.create_table('eval_scores',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('experiment_id', sa.String(length=36), nullable=False),
    sa.Column('arm_label', sa.String(length=16), nullable=False),
    sa.Column('scorer_version', sa.String(length=64), nullable=False),
    sa.Column('prs', sa.Integer(), nullable=False),
    sa.Column('recall', sa.Float(), nullable=True),
    sa.Column('precision', sa.Float(), nullable=True),
    sa.Column('unique_valid', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Numeric(precision=14, scale=6), nullable=True),
    sa.Column('latency_s', sa.Float(), nullable=True),
    sa.Column('noise_floor', sa.JSON(), nullable=True),
    _ts('computed_at', False),
    sa.CheckConstraint(_in('arm_label', ('primary', 'variant', 'control')), name=op.f('ck_eval_scores_arm_label')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_eval_scores_team_id_teams')),
    sa.ForeignKeyConstraint(['experiment_id'], ['eval_experiments.id'],
                            name=op.f('fk_eval_scores_experiment_id_eval_experiments')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_eval_scores')),
    sa.UniqueConstraint('experiment_id', 'arm_label', 'scorer_version',
                        name=op.f('uq_eval_scores_experiment_id_arm_label_scorer_version'))
    )
    op.create_table('eval_recommendations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('team_id', sa.String(length=36), nullable=False),
    sa.Column('experiment_id', sa.String(length=36), nullable=False),
    sa.Column('specialist', sa.String(length=64), nullable=False),
    sa.Column('change', sa.JSON(), nullable=False),
    sa.Column('sentence', sa.Text(), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=True),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('decided_by', sa.String(length=36), nullable=True),
    _ts('decided_at', True),
    sa.Column('applied_version', sa.JSON(), nullable=True),
    _ts('created_at', False),
    sa.CheckConstraint(_in('state', ('waiting', 'approved', 'dismissed', 'applied')),
                       name=op.f('ck_eval_recommendations_state')),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_eval_recommendations_team_id_teams')),
    sa.ForeignKeyConstraint(['experiment_id'], ['eval_experiments.id'],
                            name=op.f('fk_eval_recommendations_experiment_id_eval_experiments')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_eval_recommendations'))
    )
    op.create_index(op.f('ix_eval_recommendations_team_id_state'), 'eval_recommendations', ['team_id', 'state'],
                    unique=False)
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('arm_id', sa.String(length=36), nullable=True))
        batch.create_index(batch.f('ix_jobs_arm_id'), ['arm_id'], unique=False)
    _sqlite_foreign_keys(True)
    _modes(MODES_AFTER)


def downgrade() -> None:
    # First, since SQLite ignores the foreign key pragma inside the transaction the delete opens.
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('jobs') as batch:
        batch.drop_index(batch.f('ix_jobs_arm_id'))
        batch.drop_column('arm_id')
    _sqlite_foreign_keys(True)
    op.execute(sa.delete(tool_calls).where(tool_calls.c.mode == 'arm'))
    _modes(MODES_BEFORE)
    op.drop_index(op.f('ix_eval_recommendations_team_id_state'), table_name='eval_recommendations')
    op.drop_table('eval_recommendations')
    op.drop_table('eval_scores')
    op.drop_index(op.f('ix_eval_judgments_experiment_id_created_at'), table_name='eval_judgments')
    op.drop_table('eval_judgments')
    op.drop_index(op.f('ix_eval_matches_experiment_id_primary_run_id'), table_name='eval_matches')
    op.drop_table('eval_matches')
    # A comparison run's spend and findings would count as a review's once the tables that set them apart are gone.
    op.execute(sa.delete(llm_calls).where(llm_calls.c.run_id.in_(
        sa.select(eval_arms.c.arm_run_id).where(eval_arms.c.arm_run_id.is_not(None)))))
    op.execute(sa.delete(finding_candidates).where(finding_candidates.c.arm_id.is_not(None)))
    op.drop_index(op.f('ix_eval_arms_arm_run_id'), table_name='eval_arms')
    op.drop_table('eval_arms')
    op.drop_index(op.f('ix_eval_experiments_team_id_status'), table_name='eval_experiments')
    op.drop_table('eval_experiments')
