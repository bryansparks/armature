"""The authoring assistant. It proposes a body and never saves one."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from armature.server import assist, defs, llm, rbac
from armature.server.app.deps import server, signed_in
from armature.server.app.http import ApiError, client_ip
from armature.server.auth import AuthError, Session

router = APIRouter()

ASSIST_PATH = "/api/assist"
ASSIST_BODY_LIMIT = 2 * defs.BODY_MAX + 64 * 1024


class AssistBody(BaseModel):
    kind: str
    id: str | None = None
    body: str
    instruction: str
    team_id: str | None = None
    alias: str | None = None
    doc_kind: str | None = None


@router.post(ASSIST_PATH)
async def post_assist(payload: AssistBody, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    srv = server(request)

    def ask() -> dict[str, Any]:
        with srv.engine.connect() as conn:
            access = rbac.load_access(conn, session.user_id, bool(session.user.is_admin))
        return assist.run(srv.engine, srv.keyring, access=access, user_id=session.user_id,
                          payload=payload.model_dump(), catalog=defs.load_catalog(srv.cfg.packs_dir),
                          now=srv.now(), ip=client_ip(request), team_daily_limit=srv.cfg.assist.team_daily_limit)

    try:
        return await asyncio.to_thread(ask)
    except (assist.AssistError, llm.LLMError) as exc:
        raise ApiError(exc.status, exc.message) from exc
    except AuthError as exc:
        raise ApiError(exc.status, exc.message, exc.headers) from exc
