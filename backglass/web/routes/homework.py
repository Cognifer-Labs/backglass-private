"""The Homework page: a month at a time, with what is due on each day and what is on it.

Read-only, and a reader rather than a store — `backglass/homework.py` carries the join
and the reconciliation; this module only hands it to a template. Same shape as
`classes.py`, so a new page costs a router and a template and no schema.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from backglass import homework
from backglass.config import Settings

#: The same reasoning as `schedule.EARLIEST_DAY`: a representable month is not a month in
#: anyone's life, and `?month=9999-12` would reach the grid builder and overflow inside
#: the day arithmetic while the page built its own "next" link.
EARLIEST = date(1900, 1, 1)
LATEST = date(2199, 12, 1)


def build_router(
    templates: Jinja2Templates,
    settings: Settings,
    get_conn: Callable[[], Any],
    today: Callable[[], date],
) -> APIRouter:
    router = APIRouter()

    @router.get("/homework", response_class=HTMLResponse)
    def month(
        request: Request,
        month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
        only: str | None = Query(None, pattern=r"^(coursework|all)$"),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        now = today()
        first = homework.month_of(now)
        if month is not None:
            year, index = (int(part) for part in month.split("-"))
            if 1 <= index <= 12:
                asked = date(year, index, 1)
                if EARLIEST <= asked <= LATEST:
                    first = asked
        view = homework.load(
            conn,
            settings,
            first,
            today=now,
            only_coursework=only == "coursework",
        )
        return templates.TemplateResponse(
            request,
            "homework.html",
            {"month": view, "today": now, "settings": settings},
        )

    return router
