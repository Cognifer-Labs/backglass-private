"""The Schedule page: one day as a timeline, one week as a grid. docs/04 read-only.

Reads the same tables the Today panel reads (`dashboard_today.sql` parameterized by
date) plus fixed events through `plan/capacity.fixed_events`, so a calendar item and
a planned block can never disagree between pages — they come from the same readers.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from backglass.config import Settings
from backglass.db import query
from backglass.ledger import USER_ID
from backglass.plan import capacity, timezones


@dataclass(frozen=True)
class DayView:
    day: date
    tz: str
    blocks: list[dict[str, Any]]
    fixed: list[capacity.FixedEvent]

    @property
    def empty(self) -> bool:
        return not self.blocks and not self.fixed


def day_view(conn: sqlite3.Connection, settings: Settings, day: date) -> DayView:
    tz = timezones.active_tz(settings, day)
    blocks = [
        dict(row)
        for row in conn.execute(
            query("dashboard_today"),
            {"user_id": USER_ID, "local_date": day.isoformat()},
        )
    ]
    return DayView(day=day, tz=tz, blocks=blocks, fixed=capacity.fixed_events(conn, day, tz))


def week_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


# ── the day timeline ──────────────────────────────────────────────────────
#
# The day view is a clock face, not a list: an hour ruler, entries sized by their
# duration, and the gaps left visible — free time is information the capacity model
# already paid for, and a list of rows hides it. One pixel per minute, so a 25-minute
# small-items block is visibly small and a 90-minute protected block visibly is not.

PX_PER_MIN = 1
#: The ruler always spans at least the default working window (docs/04 §1.2), widened
#: to fit anything scheduled outside it.
WINDOW_START_H = 8
WINDOW_END_H = 19


@dataclass(frozen=True)
class Entry:
    title: str
    kind: str  # fixed | work | protected | small | buffer
    start_label: str
    end_label: str
    top: int
    height: int
    lane: int
    outcome: str = ""
    travel: bool = False

    @property
    def slim(self) -> bool:
        """Under 46px the three text lines cannot fit; render one compressed line."""
        return self.height < 46


@dataclass(frozen=True)
class Timeline:
    hours: list[int]
    height: int
    entries: list[Entry]
    now_top: int | None


def _minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def timeline(view: DayView, *, today: date) -> Timeline:
    raw: list[tuple[int, int, str, str, str, bool]] = []
    for e in view.fixed:
        raw.append(
            (
                e.starts_at.hour * 60 + e.starts_at.minute,
                max(e.minutes, 1),
                e.title or "Busy",
                "fixed",
                "",
                e.travel,
            )
        )
    for b in view.blocks:
        start = _minutes(b["starts_at"][11:16])
        end = _minutes(b["ends_at"][11:16])
        raw.append(
            (start, max(end - start, 1), str(b["title"]), str(b["kind"]),
             str(b["outcome"]), False)
        )
    raw.sort(key=lambda r: (r[0], -r[1]))

    window = (WINDOW_START_H * 60, WINDOW_END_H * 60)
    start_min = min([window[0], *(r[0] for r in raw)]) if raw else window[0]
    end_min = max([window[1], *(r[0] + r[1] for r in raw)]) if raw else window[1]
    start_min = (start_min // 60) * 60
    end_min = ((end_min + 59) // 60) * 60

    entries: list[Entry] = []
    # Two lanes: P5 bans overlapping blocks, but two fixed events can collide.
    lane_ends = [0, 0]
    for start, dur, title, kind, outcome, travel in raw:
        lane = 0 if start >= lane_ends[0] else 1
        lane_ends[lane] = max(lane_ends[lane], start + dur)
        entries.append(
            Entry(
                title=title,
                kind=kind,
                start_label=f"{start // 60:02d}:{start % 60:02d}",
                end_label=f"{(start + dur) // 60:02d}:{(start + dur) % 60:02d}",
                top=(start - start_min) * PX_PER_MIN,
                height=max(dur * PX_PER_MIN, 14),
                lane=lane,
                outcome=outcome,
                travel=travel,
            )
        )

    now_top: int | None = None
    if view.day == today:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo(view.tz))
        now_min = now.hour * 60 + now.minute
        if start_min <= now_min <= end_min:
            now_top = (now_min - start_min) * PX_PER_MIN

    return Timeline(
        hours=list(range(start_min // 60, end_min // 60 + 1)),
        height=(end_min - start_min) * PX_PER_MIN,
        entries=entries,
        now_top=now_top,
    )


def build_router(
    templates: Jinja2Templates,
    settings: Settings,
    get_conn: Callable[[], Any],
    today: Callable[[], date],
) -> APIRouter:
    router = APIRouter()

    @router.get("/schedule", response_class=HTMLResponse)
    def schedule(
        request: Request,
        date_: str | None = Query(None, alias="date"),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        day = date.fromisoformat(date_) if date_ else today()
        view = day_view(conn, settings, day)
        return templates.TemplateResponse(
            request,
            "schedule.html",
            {
                "view": view,
                "tl": timeline(view, today=today()),
                "cap": view.blocks[0] if view.blocks else None,
                "prev": (day - timedelta(days=1)).isoformat(),
                "next": (day + timedelta(days=1)).isoformat(),
                "week_start": week_of(day).isoformat(),
                "settings": settings,
            },
        )

    @router.get("/schedule/week", response_class=HTMLResponse)
    def week(
        request: Request,
        start: str | None = None,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        first = week_of(date.fromisoformat(start) if start else today())
        days = [day_view(conn, settings, first + timedelta(days=i)) for i in range(7)]
        return templates.TemplateResponse(
            request,
            "schedule_week.html",
            {
                "days": days,
                "first": first,
                "prev": (first - timedelta(days=7)).isoformat(),
                "next": (first + timedelta(days=7)).isoformat(),
                "today": today(),
                "settings": settings,
            },
        )

    return router
