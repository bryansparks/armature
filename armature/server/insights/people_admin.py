"""A team lead's corrections to people: merging two people who are one, and deleting a person entirely."""
from __future__ import annotations

from typing import Any

from sqlalchemy import Column, Connection, delete, select, update

from armature.server import audit
from armature.server.db.schema import metadata, people, person_identities
from armature.server.insights.attribution import ARMATURE


class PeopleError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def person_columns() -> list[Column]:
    """Every gh_ column that points at a person, so a later table's column is covered without a change here."""
    return [column for table in metadata.sorted_tables if table.name.startswith("gh_")
            for column in table.c if column.name.endswith("_person_id")]


def get(conn: Connection, team_id: str, person_id: str, *, locked: bool = False) -> Any:
    query = select(people).where(people.c.id == person_id, people.c.team_id == team_id)
    row = conn.execute(query.with_for_update() if locked else query).first()
    if row is None:
        raise PeopleError(404, "No such person.")
    return row


def _armatures(conn: Connection, team_id: str, person_id: str) -> bool:
    return conn.execute(select(person_identities.c.id).where(
        person_identities.c.team_id == team_id, person_identities.c.person_id == person_id,
        person_identities.c.kind == ARMATURE)).first() is not None


def _editable(conn: Connection, team_id: str, person_id: str) -> Any:
    row = get(conn, team_id, person_id, locked=True)
    if _armatures(conn, team_id, person_id):
        raise PeopleError(409, "Armature's own record can't be merged or deleted.")
    return row


def _repoint(conn: Connection, team_id: str, source: str, into: str | None) -> dict[str, int]:
    moved = {}
    for column in person_columns():
        table = column.table
        count = conn.execute(update(table).where(table.c.team_id == team_id, column == source)
                             .values({column.name: into})).rowcount
        if count:
            moved[f"{table.name}.{column.name}"] = count
    return moved


def merge(conn: Connection, team_id: str, source_id: str, into_id: Any, *, actor_id: str, ip: str | None) -> Any:
    """Move the source's identities and gh_ rows to ``into_id``, then delete the source. Returns the kept person."""
    if not isinstance(into_id, str) or not into_id:
        raise PeopleError(400, "into must be a person id.")
    if into_id == source_id:
        raise PeopleError(400, "A person can't be merged into themselves.")
    source = _editable(conn, team_id, source_id)
    into = _editable(conn, team_id, into_id)
    if into.kind != "human":
        raise PeopleError(409, "A person can only be merged into a human, not a bot.")
    identities = conn.execute(update(person_identities).where(person_identities.c.team_id == team_id,
                                                              person_identities.c.person_id == source_id)
                              .values(person_id=into_id)).rowcount
    moved = _repoint(conn, team_id, source_id, into_id)
    if into.user_id is None and source.user_id is not None:
        conn.execute(update(people).where(people.c.id == into_id).values(user_id=source.user_id))
    conn.execute(delete(people).where(people.c.id == source_id, people.c.team_id == team_id))
    audit.record(conn, action="insights.person.merge", actor_id=actor_id, team_id=team_id, target_kind="person",
                 target_id=source_id, detail={"into": into_id, "identities": identities, "rows": moved}, ip=ip)
    return get(conn, team_id, into_id)


def remove(conn: Connection, team_id: str, person_id: str, *, actor_id: str, ip: str | None) -> None:
    """Delete the person and their identities, leaving their gh_ rows with no person."""
    _editable(conn, team_id, person_id)
    moved = _repoint(conn, team_id, person_id, None)
    identities = conn.execute(delete(person_identities).where(person_identities.c.team_id == team_id,
                                                              person_identities.c.person_id == person_id)).rowcount
    conn.execute(delete(people).where(people.c.id == person_id, people.c.team_id == team_id))
    audit.record(conn, action="insights.person.delete", actor_id=actor_id, team_id=team_id, target_kind="person",
                 target_id=person_id, detail={"identities": identities, "rows": moved}, ip=ip)


def public(row: Any) -> dict[str, Any]:
    return {"id": row.id, "name": row.display_name, "kind": row.kind, "user_id": row.user_id}
