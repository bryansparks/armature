"""The host's status, as the configured status command last reported it."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from armature.server.app.deps import signed_in
from armature.server.app.http import ApiError
from armature.server.auth import Session

router = APIRouter()


@router.get("/api/status")
def get_status(request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    # Host state spans every team's repositories and logs, so only admins see it.
    if not session.user.is_admin:
        raise ApiError(403, "Only an admin can see the host's status.")
    hook = getattr(request.app.state, "status", None)
    if hook is None:
        return {"configured": False, "markdown": None, "updated_at": None, "error": None}
    return hook.snapshot()
