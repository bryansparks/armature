"""The daily report, instance-wide, so only an admin reads it."""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from armature.server import display
from armature.server.app.deps import Granted, require, server
from armature.server.app.http import ApiError
from armature.server.db.schema import daily_reports
from armature.server.insights import report

router = APIRouter()

BASE = "/api/insights/daily-reports"


@router.get(BASE)
def list_reports(request: Request, granted: Granted = Depends(require("admin"))) -> dict[str, Any]:
    with server(request).engine.connect() as conn:
        rows = conn.execute(select(daily_reports.c.day, daily_reports.c.created_at)
                            .order_by(daily_reports.c.day.desc())).all()
        zone = display.load(conn)["timezone"]
    return {"days": [{"day": r.day.isoformat(), "created_at": r.created_at} for r in rows], "timezone": zone,
            "builds_after_hour": int(report.MARGIN.total_seconds() // 3600)}


@router.get(BASE + "/{day}")
def get_report(day: str, request: Request, granted: Granted = Depends(require("admin"))) -> dict[str, Any]:
    try:
        wanted = date.fromisoformat(day)
    except ValueError as exc:
        raise ApiError(400, "day must be a date such as 2026-11-16.") from exc
    with server(request).engine.connect() as conn:
        row = conn.execute(select(daily_reports.c.day, daily_reports.c.created_at, daily_reports.c.data)
                           .where(daily_reports.c.day == wanted)).first()
    if row is None:
        raise ApiError(404, "No report for that day.")
    return {"day": row.day.isoformat(), "created_at": row.created_at, "data": row.data}
