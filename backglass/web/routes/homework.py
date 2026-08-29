"""The Homework page, in two registers over the same rows.

**The to-do list is what `/homework` serves**, because it answers the question the tab is
opened with. Owner, 2026-08-29: *"i want a homework todo view that ranks all homework by
due date and time it takes."* A month grid is a shape and has no top; a ranked list has a
first row, which is the whole point of ranking.

**The month grid keeps every URL it had.** `?month=YYYY-MM` still renders it, so the links
`/schedule`, `/schedule/week` and `/classes` already carry go on landing where they always
did — naming a month is asking for the month. `?view=month` reaches it without naming one,
and `?view=list` names the list explicitly.

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
        # Which register. Defaulted from whether a month was named rather than to a
        # constant: a link that says `month=2026-09` is asking for September, and one
        # that says nothing is asking what to do next.
        view: str | None = Query(None, pattern=r"^(list|month)$"),
        only: str | None = Query(None, pattern=r"^(coursework|all)$"),
        # The course code as the registrar issues it, in any of the spellings a link
        # might carry it in — `CHM 113`, `CHM113`, `chm113`. Anything else is not a
        # course and is refused at the door rather than silently emptying the page.
        course: str | None = Query(None, pattern=r"^[A-Za-z]{2,4}[ -]?\d{3}$"),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        now = today()
        if view == "list" or (view is None and month is None):
            # The list opens on coursework because it is the Homework tab. Unfiltered it
            # ran to 300 items and its top row was a hall parking permit due 6 July —
            # real, owed, and not homework. `?only=all` is one click away and the page
            # prints how many rows that click would add, so the narrowing is stated
            # rather than silent. The month grid keeps its own default: it is a calendar
            # of the owner's obligations and always has been.
            todo = homework.todo(
                conn,
                settings,
                today=now,
                only_coursework=only != "all",
                course=course,
            )
            return templates.TemplateResponse(
                request,
                "homework_todo.html",
                {"todo": todo, "today": now, "settings": settings},
            )
        first = homework.month_of(now)
        if month is not None:
            year, index = (int(part) for part in month.split("-"))
            if 1 <= index <= 12:
                asked = date(year, index, 1)
                if EARLIEST <= asked <= LATEST:
                    first = asked
        grid = homework.load(
            conn,
            settings,
            first,
            today=now,
            only_coursework=only == "coursework",
            course=course,
        )
        return templates.TemplateResponse(
            request,
            "homework.html",
            {"month": grid, "today": now, "settings": settings},
        )

    return router
