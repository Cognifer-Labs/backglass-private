"""The Memory page: the personal knowledge base, grouped by subject. Phase 12.

Read plus two writes (add, retract). Supersession happens in facts.remember, so the
page never edits a row in place — updating a fact is adding its replacement.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from backglass import facts
from backglass.config import Settings


def build_router(
    templates: Jinja2Templates,
    settings: Settings,
    get_conn: Callable[[], Any],
    today: Callable[[], date],
) -> APIRouter:
    del today  # facts stamp their own local time
    router = APIRouter()

    def page(request: Request, conn: sqlite3.Connection) -> Any:
        all_facts = facts.recall(conn)
        subjects: dict[str, list[facts.Fact]] = {}
        for f in all_facts:
            subjects.setdefault(f.subject, []).append(f)
        return templates.TemplateResponse(
            request,
            "memory.html",
            {"subjects": subjects, "count": len(all_facts), "settings": settings},
        )

    @router.get("/memory", response_class=HTMLResponse)
    def memory(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
        return page(request, conn)

    @router.post("/memory", response_class=HTMLResponse)
    def add(
        request: Request,
        subject: str = Form(...),
        key: str = Form(...),
        value: str = Form(...),
        note: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        try:
            facts.remember(conn, settings, subject, key, value, note=note, source="manual")
        except facts.FactError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return page(request, conn)

    @router.post("/memory/{fact_id}/forget", response_class=HTMLResponse)
    def forget(
        fact_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Any:
        try:
            facts.forget(conn, fact_id)
        except facts.FactError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return page(request, conn)

    return router
