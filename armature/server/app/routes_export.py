"""The state of the export that follows each definition change (``export_on_change``)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from armature.server.app.deps import Granted, require

router = APIRouter()


@router.get("/api/export/status")
def export_status(request: Request, granted: Granted = Depends(require("admin"))) -> dict[str, Any]:
    hook = getattr(request.app.state, "export", None)
    if hook is None:
        return {"configured": False}
    return hook.status()
