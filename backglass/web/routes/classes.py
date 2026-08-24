"""The Classes page: the semester as the ledger holds it, one card per course.

Read-only, and a reader rather than a store — `backglass/courses.py` carries the join and
the reasoning; this module only hands it to a template. Same shape as the other pages, so
a new page costs a router and a template and no schema.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from backglass import courses
from backglass.config import Settings


def build_router(
    templates: Jinja2Templates,
    settings: Settings,
    get_conn: Callable[[], Any],
    today: Callable[[], date],
) -> APIRouter:
    router = APIRouter()

    @router.get("/classes", response_class=HTMLResponse)
    def classes(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
        found = courses.load(conn, settings)
        return templates.TemplateResponse(
            request,
            "classes.html",
            {
                "courses": found,
                "today": today(),
                "semester_dates": courses.semester_dates(conn, settings),
                # Named on the page rather than left to be discovered: an owner who put
                # 28 dates on a calendar and sees 13 here should be told why by the page
                # that is short, not by reading the connector.
                "all_day_note": (
                    "Two reasons this list is shorter than the calendar. All-day rows — "
                    "the drop and withdrawal deadlines, the no-class days, the VR pod "
                    "booking reminders — never enter the ledger at all: apple_calendar "
                    "drops them at ingest because an all-day row is not capacity "
                    "(docs/07). And a timed exam only appears once it is inside the "
                    "connector's 21-day horizon, so an exam in November arrives in "
                    "November. The durable record of a syllabus date is the commitment "
                    "extraction made from the syllabus itself, which is what the course "
                    "cards above list."
                ),
                "settings": settings,
            },
        )

    return router
