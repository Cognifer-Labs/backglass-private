"""The Conversations page: what the ledger reads, and what it is waiting to be told.

Read plus one write. The write is a decision, not an edit — a chat is monitored or
ignored, and `chats.decide` records which and when. There is no delete: an ignored
conversation stays in the table precisely so it is never asked about again.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from backglass import chats as chats_mod
from backglass.config import Settings
from backglass.web.params import RowId


def build_router(
    templates: Jinja2Templates,
    settings: Settings,
    get_conn: Callable[[], Any],
    today: Callable[[], date],
) -> APIRouter:
    del today
    router = APIRouter()

    def page(request: Request, conn: sqlite3.Connection) -> Any:
        rows = chats_mod.listing(conn)
        return templates.TemplateResponse(
            request,
            "chats.html",
            {
                "chats": rows,
                "waiting": [c for c in rows if c.undecided],
                "monitored": [c for c in rows if c.decision == chats_mod.MONITOR],
                "ignored": [c for c in rows if c.decision == chats_mod.IGNORE],
                "settings": settings,
            },
        )

    @router.get("/chats", response_class=HTMLResponse)
    def chats_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
        return page(request, conn)

    @router.post("/chats/{chat_id}/{decision}", response_class=HTMLResponse)
    def decide(
        chat_id: RowId,
        decision: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        try:
            chats_mod.decide(conn, chat_id, decision)
        except ValueError as exc:
            # The decision is a path segment, so anything can arrive here.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        conn.commit()
        return RedirectResponse(url="/chats", status_code=303)

    return router
