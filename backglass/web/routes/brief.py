"""The Brief page: reading a generated brief on the machine that generated it.

docs/05 gives the brief one delivery path — a transactional provider, at 06:00, by
email. That path is configuration the owner may never have completed, and until they
do, `daily.persist` writes the brief into SQLite and nothing reads it back: the only
brief route the app had was the tracking pixel, which records that an *email* was
opened. A brief nobody can open is the failure docs/05 B6 exists to prevent, arriving
by a different road.

So this page is not a second brief format. It renders the stored `content_md` through
`render.parse_markdown`, which returns the same `Brief` object the email was built
from — same sections, same order, same status chips, same source links. What the page
adds is what only the dashboard can say: whether the thing was ever delivered, and
which other days have one.

Loopback-only like every other page (web/security.py). Read-only: nothing here writes,
so nothing here is subject to the cross-site write guard.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from backglass.brief import deliver, render
from backglass.config import Settings
from backglass.db import query
from backglass.ledger import USER_ID


def delivery_state(row: dict[str, Any]) -> str:
    """What happened to this brief, in the words the row can support.

    The `brief` table records `sent_at` and nothing about a failure, so a brief that
    was never sent gets "not delivered" and no reason. Reaching for the *current*
    configuration to explain a *past* row would be an invented cause — today's missing
    BRIEF_TO is not evidence about what happened in July. The configuration note on the
    page is a separate statement and reads as one.
    """
    if row["sent_at"]:
        return f"delivered {str(row['sent_at'])[:16].replace('T', ' ')}"
    return "not delivered"


def build_router(
    templates: Jinja2Templates,
    settings: Settings,
    get_conn: Callable[[], Any],
    today: Callable[[], date],
) -> APIRouter:
    del today  # a brief is dated by the day it was generated for, not by now
    router = APIRouter()

    def page(
        request: Request,
        conn: sqlite3.Connection,
        row: dict[str, Any] | None,
        *,
        anchor: str,
        missing: str,
        status_code: int,
    ) -> Any:
        """One brief, or an honest account of why there is none to show.

        The prev/next links are computed around `anchor` even when there is no brief
        there, so a day that was never generated still offers the way out to the days
        that were.
        """
        neighbours = conn.execute(
            query("brief_neighbours"), {"user_id": USER_ID, "on_date": anchor}
        ).fetchone()
        return templates.TemplateResponse(
            request,
            "brief.html",
            {
                "row": row,
                "brief": render.parse_markdown(str(row["content_md"]), kind=str(row["kind"]))
                if row is not None
                else None,
                "base": settings.dashboard_base_url,
                "delivery": delivery_state(row) if row is not None else None,
                # Why no brief will ever arrive by email, when that is true today. A
                # statement about configuration, kept apart from the row's own history.
                "cannot_deliver": deliver.unconfigured(settings),
                "neighbours": neighbours,
                "missing": missing,
                "settings": settings,
            },
            status_code=status_code,
        )

    @router.get("/brief", response_class=HTMLResponse)
    def latest(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
        """The most recent brief. The page the nav link opens.

        200 even when there is none: /brief is where briefs live whether or not one has
        been generated yet, so a fresh install's nav link lands on an empty state rather
        than on a 404.
        """
        row = conn.execute(query("brief_latest"), {"user_id": USER_ID}).fetchone()
        return page(
            request,
            conn,
            row,
            anchor=str(row["generated_for_date"]) if row is not None else date.min.isoformat(),
            missing="No brief has been generated yet.",
            status_code=200,
        )

    @router.get("/brief/{on_date}", response_class=HTMLResponse)
    def on_date(
        on_date: date, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Any:
        """One day's brief. 404 when that day has none — the URL named a day, and the
        honest answer is that nothing was written for it."""
        row = conn.execute(
            query("brief_on_date"), {"user_id": USER_ID, "on_date": on_date.isoformat()}
        ).fetchone()
        return page(
            request,
            conn,
            row,
            anchor=on_date.isoformat(),
            missing=f"No brief for {on_date.isoformat()}.",
            status_code=200 if row is not None else 404,
        )

    return router
