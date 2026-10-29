"""Workflow kind: what a workflow's runs count as, so a rename changes only its label.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-27 12:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0009'
down_revision: str | Sequence[str] | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = ('review', 'fix', 'merge', 'port', 'work', 'other')
# The names the server classified by before this revision, frozen here.
KIND_BY_NAME = {
    'review': ('code-review', 'pr-role-review', 'pr-review'),
    'fix': ('pr-fix',),
    'merge': ('pr-merged',),
    'port': ('ios-port',),
    'work': ('dev-work', 'jira-work'),
}
INSTANCE_SCOPE = '*'
CHUNK = 500

workflows = sa.table('workflows', sa.column('id', sa.String), sa.column('scope', sa.String),
                     sa.column('name', sa.String), sa.column('kind', sa.String))
runs = sa.table('runs', sa.column('id', sa.String), sa.column('team_id', sa.String),
                sa.column('project_id', sa.String), sa.column('workflow_id', sa.String),
                sa.column('workflow_name', sa.String))


# As in 0004: SQLite rebuilds workflows, and runs, jobs, and triggers reference it.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def _resolve(conn: sa.Connection, name: str, team_id: str | None, project_id: str | None) -> str | None:
    order = [s for s in (project_id, team_id, INSTANCE_SCOPE) if s is not None]
    found = {r.scope: r for r in conn.execute(sa.select(workflows.c.id, workflows.c.scope, workflows.c.name)
                                              .where(workflows.c.name == name, workflows.c.scope.in_(order)))
             if r.name == name}
    return next((found[s].id for s in order if s in found), None)


def _link_runs(conn: sa.Connection) -> None:
    # Grouped here, not in SQL, so a case-insensitive MySQL collation can't merge two names.
    unlinked = conn.execute(sa.select(runs.c.id, runs.c.team_id, runs.c.project_id, runs.c.workflow_name)
                            .where(runs.c.workflow_id.is_(None), runs.c.workflow_name.is_not(None))).all()
    groups: dict[tuple, list[str]] = {}
    for row in unlinked:
        groups.setdefault((row.team_id, row.project_id, row.workflow_name), []).append(row.id)
    for (team_id, project_id, name), run_ids in groups.items():
        workflow_id = _resolve(conn, name, team_id, project_id)
        if workflow_id is None:
            continue
        for start in range(0, len(run_ids), CHUNK):
            conn.execute(sa.update(runs).where(runs.c.id.in_(run_ids[start:start + CHUNK]),
                                               runs.c.workflow_id.is_(None)).values(workflow_id=workflow_id))


def upgrade() -> None:
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('workflows') as batch:
        batch.add_column(sa.Column('kind', sa.String(length=16), nullable=False, server_default='other'))
        batch.create_check_constraint(op.f('ck_workflows_kind'),
                                      f"kind IN ({', '.join(repr(k) for k in KINDS)})")
    _sqlite_foreign_keys(True)
    conn = op.get_bind()
    for kind, names in KIND_BY_NAME.items():
        conn.execute(sa.update(workflows).where(workflows.c.name.in_(names)).values(kind=kind))
    _link_runs(conn)


def downgrade() -> None:
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('workflows') as batch:
        batch.drop_constraint(op.f('ck_workflows_kind'), type_='check')
        batch.drop_column('kind')
    _sqlite_foreign_keys(True)
