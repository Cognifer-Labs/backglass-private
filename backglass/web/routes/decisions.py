"""The Decisions page: the major choices the owner has settled, newest first.

Read plus two writes (record, revisit). Supersession happens in decisions.record, so
the page never edits a row in place — changing a decision is recording its
replacement. Recording can also close the open commitment it settles, which is why
the add form carries a select over the open ledger.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from backglass import decisions
from backglass.config import Settings
from backglass.ledger import USER_ID


def build_router(
    templates: Jinja2Templates,
    settings: Settings,
    get_conn: Callable[[], Any],
    today: Callable[[], date],
) -> APIRouter:
    del today  # decisions stamp their own local time
    router = APIRouter()

    def page(request: Request, conn: sqlite3.Connection) -> Any:
        standing = decisions.active(conn)
        open_commitments = conn.execute(
            "SELECT id, what FROM commitment WHERE user_id = ? AND status = 'open'"
            " ORDER BY what",
            (USER_ID,),
        ).fetchall()
        return templates.TemplateResponse(
            request,
            "decisions.html",
            {
                "decisions": standing,
                "count": len(standing),
                "open_commitments": open_commitments,
                "settings": settings,
            },
        )

    @router.get("/decisions", response_class=HTMLResponse)
    def decisions_page(
        request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Any:
        return page(request, conn)

    @router.post("/decisions", response_class=HTMLResponse)
    def record(
        request: Request,
        title: str = Form(...),
        choice: str = Form(...),
        reasoning: str = Form(""),
        commitment: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        commitment_id: int | None = None
        if commitment.strip():
            try:
                commitment_id = int(commitment)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="bad commitment id") from exc
        try:
            decisions.record(
                conn,
                settings,
                title,
                choice,
                reasoning=reasoning,
                commitment_id=commitment_id,
            )
        except decisions.DecisionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return page(request, conn)

    @router.post("/decisions/{decision_id}/revisit", response_class=HTMLResponse)
    def revisit(
        decision_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Any:
        try:
            decisions.revisit(conn, decision_id)
        except decisions.DecisionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return page(request, conn)

    return router
