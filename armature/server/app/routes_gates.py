"""Gates a run is waiting at, and approving or rejecting them.

A project lead on the gate's project may decide it, unless the gate asks for a
higher role. The gate's team and project come from its row, never the body.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from armature.server import gates, rbac
from armature.server.app.deps import (
    Granted,
    ScopeFrom,
    by_run,
    require,
    require_step_up,
    server,
    signed_in,
)
from armature.server.app.http import ApiError, client_ip
from armature.server.auth import Session
from armature.server.db.schema import GATE_STATES

router = APIRouter()


class DecisionBody(BaseModel):
    nonce: str
    note: str | None = None


def by_gate(param: str = "gate_id") -> ScopeFrom:
    return lambda conn, params: gates.target(conn, params[param])


@router.get("/api/gates")
def list_gates(request: Request, state: str | None = Query(None, max_length=16),
               limit: int = Query(100, ge=1, le=500), session: Session = Depends(signed_in)) -> dict[str, Any]:
    if state is not None and state not in GATE_STATES:
        raise ApiError(400, f"state is one of {', '.join(GATE_STATES)}.")
    with server(request).engine.connect() as conn:
        access = rbac.load_access(conn, session.user_id, bool(session.user.is_admin))
        found = gates.list_gates(conn, rbac.visible_scope(access), state=state, limit=limit)
    return {"gates": [_shown(access, g) for g in found]}


@router.get("/api/gates/{gate_id}")
def get_gate(gate_id: str, request: Request,
             granted: Granted = Depends(require("viewer", by_gate()))) -> dict[str, Any]:
    with server(request).engine.connect() as conn:
        gate = gates.get_gate(conn, gate_id)
    return {"gate": _shown(granted.access, gate)}


@router.get("/api/runs/{run_id}/gates")
def run_gates(run_id: str, request: Request,
              granted: Granted = Depends(require("viewer", by_run()))) -> dict[str, Any]:
    with server(request).engine.connect() as conn:
        found = gates.list_gates(conn, None, run_id=run_id)
    return {"gates": [_shown(granted.access, g) for g in found]}


def _may_decide(access: rbac.Access, gate: dict[str, Any]) -> bool:
    role = rbac.role_for(access, rbac.Target(team_id=gate["team_id"], project_id=gate["project_id"]))
    return gate["state"] == "open" and rbac.at_least(role, "project_lead") and rbac.at_least(role, gate["min_role"])


def _shown(access: rbac.Access, gate: dict[str, Any]) -> dict[str, Any]:
    """The gate as this viewer sees it. Only someone who may decide it gets the nonce a decision needs."""
    may_decide = _may_decide(access, gate)
    return {**{k: v for k, v in gate.items() if may_decide or k != "nonce"}, "may_decide": may_decide}


def _decide(request: Request, granted: Granted, gate_id: str, body: DecisionBody, approved: bool) -> dict[str, Any]:
    srv = server(request)
    with srv.engine.connect() as conn:
        gate = gates.get_gate(conn, gate_id)
    if not rbac.at_least(granted.role, gate["min_role"]):
        raise ApiError(403, "Not allowed.")
    if rbac.RANK[gate["min_role"]] >= rbac.RANK["team_lead"]:
        require_step_up(request, granted.session)
    try:
        with srv.engine.begin() as conn:
            decided = gates.decide(conn, gate_id=gate_id, approved=approved, nonce=body.nonce,
                                   actor_id=granted.user_id, via="web", note=body.note, now=srv.now(),
                                   ip=client_ip(request))
    except gates.GateError as exc:
        raise ApiError(exc.status, exc.message) from exc
    return {"gate": _shown(granted.access, decided)}


@router.post("/api/gates/{gate_id}/approve")
def approve_gate(gate_id: str, body: DecisionBody, request: Request,
                 granted: Granted = Depends(require("project_lead", by_gate()))) -> dict[str, Any]:
    return _decide(request, granted, gate_id, body, True)


@router.post("/api/gates/{gate_id}/reject")
def reject_gate(gate_id: str, body: DecisionBody, request: Request,
                granted: Granted = Depends(require("project_lead", by_gate()))) -> dict[str, Any]:
    return _decide(request, granted, gate_id, body, False)
