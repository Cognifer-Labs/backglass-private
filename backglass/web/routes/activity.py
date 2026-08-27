"""The Activity page: what changed, what was withdrawn, and what the system said.

Read-only, and a reader rather than a store — `backglass/activity.py` carries the merge
and the reasoning; this module only hands it to a template. Same shape as `classes.py`,
so the page costs a router and a template and no schema.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from backglass import activity
from backglass.config import Settings

#: The windows the page offers. A fixed set rather than a free integer: the query string
#: is a URL the owner may edit, and `?days=100000` should not be a way to ask the page to
#: render the whole ledger.
WINDOWS = (7, 30, 90)


def build_router(
    templates: Jinja2Templates,
    settings: Settings,
    get_conn: Callable[[], Any],
    today: Callable[[], date],
) -> APIRouter:
    router = APIRouter()

    @router.get("/activity", response_class=HTMLResponse)
    def activity_page(
        request: Request,
        days: int = Query(activity.DEFAULT_DAYS),
        stream: str | None = Query(None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        # An out-of-range window falls back to the default rather than 422ing. This is a
        # nav destination, and a page that refuses to render because a query string was
        # hand-edited is worse than one that shows the default and says which it used.
        window = days if days in WINDOWS else activity.DEFAULT_DAYS
        feed = activity.load(conn, days=window, stream=stream)
        return templates.TemplateResponse(
            request,
            "activity.html",
            {
                "feed": feed,
                "today": today(),
                "windows": WINDOWS,
                "streams": activity.STREAMS,
                "settings": settings,
            },
        )

    return router
