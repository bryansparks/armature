"""Run thread: the Jira issue a run's work belongs to, so a pull request's runs group with the issue's.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-27 20:00:00

"""
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0011'
down_revision: str | Sequence[str] | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The rules armature.server.threads applied at this revision, frozen here.
KEY = r"[A-Z][A-Z0-9_]{1,100}-[1-9][0-9]{0,9}"
ISSUE_KEY = re.compile(KEY)
TITLE_KEY = re.compile(rf"\[(?P<key>{KEY})\]")
PULL_SUBJECT = re.compile(r"^(?P<repo>[^#\s]+/[^#\s]+)#(?P<number>[1-9][0-9]{0,9})$")
PULL_URL = re.compile(r"https://github\.com/(?P<repo>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)"
                      r"/pull/(?P<number>[1-9][0-9]{0,9})/?")
PULL_LABEL = "pull request"
CHUNK = 500

runs = sa.table('runs', sa.column('id', sa.String), sa.column('team_id', sa.String),
                sa.column('subject', sa.String), sa.column('summary', sa.JSON), sa.column('thread', sa.String),
                sa.column('started_at', sa.DateTime))


def _thread(subject: str, summary) -> str | None:
    if ISSUE_KEY.fullmatch(subject):
        return subject
    title = summary.get("title") if isinstance(summary, dict) else None
    match = TITLE_KEY.match(title) if isinstance(title, str) and PULL_SUBJECT.fullmatch(subject) else None
    return match["key"] if match else None


def _pulls(summary) -> list[str]:
    links = summary.get("links") if isinstance(summary, dict) else None
    pulls = []
    for link in links if isinstance(links, list) else []:
        label, url = (link.get("label"), link.get("url")) if isinstance(link, dict) else (None, None)
        match = PULL_URL.fullmatch(url) if isinstance(label, str) and isinstance(url, str) else None
        if match and label.strip().lower() == PULL_LABEL:
            pulls.append(f"{match['repo']}#{match['number']}")
    return pulls


def _backfill(conn: sa.Connection) -> None:
    rows = conn.execute(sa.select(runs.c.id, runs.c.team_id, runs.c.subject, runs.c.summary)
                        .where(runs.c.subject.is_not(None)).order_by(runs.c.started_at, runs.c.id)).all()
    threaded: dict[str, str] = {}
    linked: dict[tuple[str, str], str] = {}
    for row in rows:
        thread = _thread(row.subject, row.summary)
        if thread is not None:
            threaded[row.id] = thread
        if ISSUE_KEY.fullmatch(row.subject):
            for pull in _pulls(row.summary):
                linked.setdefault((row.team_id, pull), row.subject)
    for row in rows:
        if row.id not in threaded and (row.team_id, row.subject) in linked:
            threaded[row.id] = linked[(row.team_id, row.subject)]
    # Runs on one pull request stay in one thread, the latest one any of them was given.
    latest = {(row.team_id, row.subject): threaded[row.id] for row in rows if row.id in threaded}
    for row in rows:
        if row.id not in threaded and (row.team_id, row.subject) in latest:
            threaded[row.id] = latest[(row.team_id, row.subject)]
    by_thread: dict[str, list[str]] = {}
    for run_id, thread in threaded.items():
        by_thread.setdefault(thread, []).append(run_id)
    for thread, run_ids in by_thread.items():
        for start in range(0, len(run_ids), CHUNK):
            conn.execute(sa.update(runs).where(runs.c.id.in_(run_ids[start:start + CHUNK])).values(thread=thread))


def upgrade() -> None:
    op.add_column('runs', sa.Column('thread', sa.String(length=191), nullable=True))
    op.create_index(op.f('ix_runs_thread_started_at'), 'runs', ['thread', 'started_at'])
    _backfill(op.get_bind())


def downgrade() -> None:
    op.drop_index(op.f('ix_runs_thread_started_at'), table_name='runs')
    with op.batch_alter_table('runs') as batch:
        batch.drop_column('thread')
