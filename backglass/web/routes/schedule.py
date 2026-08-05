"""The Schedule page: one day as a timeline, one week as a grid. docs/04 read-only.

Reads the same tables the Today panel reads (`dashboard_today.sql` parameterized by
date) plus fixed events through `plan/capacity.fixed_events`, so a calendar item and
a planned block can never disagree between pages — they come from the same readers.

The two readers overlap by design and describe the same event twice; `_collapse` is the
single merge point that makes it draw once.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from annotated_types import Ge, Le
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


#: The window these two pages will render. `date.min`/`date.max` are representable and
#: are not days in anyone's life: `?date=9999-12-31` parsed fine, reached the handler,
#: and then raised OverflowError inside `timezones.day_bounds` — which adds a day to
#: find the day's end — while the page was building its own "next" link. Bounding the
#: parameter is one guard at the door rather than clamped arithmetic at each of the
#: three places that does date maths downstream.
EARLIEST_DAY = date(1900, 1, 1)
LATEST_DAY = date(2200, 1, 1)

#: The bound travels with the type rather than being spelled out at each of the two
#: routes. `Query(ge=…)` takes numbers, so the constraint is expressed the way pydantic
#: expresses one over any ordered type.
DayParam = Annotated[date, Ge(EARLIEST_DAY), Le(LATEST_DAY)]


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

    @property
    def tiny(self) -> bool:
        """Under 32px even one 13px line plus its keylines does not fit. Rather than
        shrink type below the 11px floor, the title is deleted from the canvas and
        moved to the title attribute: stripe plus start time is all that renders."""
        return self.height < 32


@dataclass(frozen=True)
class Timeline:
    hours: list[int]
    height: int
    entries: list[Entry]
    now_top: int | None


def _minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def _clock(minute_of_day: int) -> str:
    return f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"


#: (start_minute, duration, title, kind, outcome, travel)
RawEntry = tuple[int, int, str, str, str, bool]


def _collapse(raw: list[RawEntry]) -> list[RawEntry]:
    """One event draws once, however many readers described it.

    The page reads two overlapping surfaces on purpose (see the module docstring), and
    both of them carry the same calendar event. `plan/planner` persists a `kind='fixed'`
    plan_block for every event in `cap.fixed`, so `dashboard_today.sql` returns it — and
    then `capacity.fixed_events` returns it again straight from the ledger. On top of
    that the ledger itself holds the ASU and Apple imports of one class, which
    `capacity._distinct` collapses inside `compute` but not on the way to this page.
    Drawn, the copies stack pixel-identically across the two lanes and the canvas asserts
    a triple-booked day under the capacity sentence that says the day fits.

    Identity is `capacity._distinct`'s, in the coordinates this page already works in:
    (start minute, duration, folded title). `capacity._aware` resolved the offsets and
    the planner wrote local ISO, so a start minute here is the same instant however the
    source spelled it. Two entries at the same minute, the same length and the same title
    are one thing to look at.

    Deduplicating here rather than filtering `kind='fixed'` blocks out is what keeps an
    engagement-derived fixed block — "dinner at seven", which has no calendar row behind
    it — on the timeline: it has no second copy, so nothing collapses it away.

    Fields are merged rather than taken from a winner, because the two readers know
    different things and each is the only one that knows its own. `capacity.fixed_events`
    records travel and the plan block does not; the plan block records the outcome and
    the calendar reader hardcodes `""`. Picking either copy whole therefore drops a fact
    that only the loser held — and since `_raw_entries` appends the calendar copies first,
    a winner-takes-all merge always loses the outcome, so a block marked done or rolled
    would silently render as if it had never been touched.
    """
    kept: dict[tuple[int, int, str], RawEntry] = {}
    for entry in raw:
        identity = (entry[0], entry[1], entry[2].casefold())
        seen = kept.get(identity)
        if seen is None:
            kept[identity] = entry
            continue
        start, dur, title, kind, outcome, travel = seen
        kept[identity] = (
            start,
            dur,
            title,
            kind,
            outcome or entry[4],
            travel or entry[5],
        )
    return list(kept.values())


def _raw_entries(view: DayView) -> list[RawEntry]:
    raw: list[RawEntry] = []
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
    raw = _collapse(raw)
    raw.sort(key=lambda r: (r[0], -r[1]))
    return raw


def _window(raws: list[list[RawEntry]]) -> tuple[int, int]:
    """Hour-snapped span covering the default working window plus anything
    scheduled outside it — across every day given, so week columns share a ruler."""
    flat = [r for raw in raws for r in raw]
    start_min = min([WINDOW_START_H * 60, *(r[0] for r in flat)])
    end_min = max([WINDOW_END_H * 60, *(r[0] + r[1] for r in flat)])
    return (start_min // 60) * 60, ((end_min + 59) // 60) * 60


def _place(raw: list[RawEntry], start_min: int, *, px: float, min_height: int) -> list[Entry]:
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
                start_label=_clock(start),
                end_label=_clock(start + dur),
                top=round((start - start_min) * px),
                height=max(round(dur * px), min_height),
                lane=lane,
                outcome=outcome,
                travel=travel,
            )
        )
    return entries


def _now_minute(view: DayView) -> int:
    now = datetime.now(ZoneInfo(view.tz))
    return now.hour * 60 + now.minute


def _now_top(view: DayView, today: date, start_min: int, end_min: int, px: float) -> int | None:
    if view.day != today:
        return None
    now_min = _now_minute(view)
    if start_min <= now_min <= end_min:
        return round((now_min - start_min) * px)
    return None


def timeline(view: DayView, *, today: date) -> Timeline:
    raw = _raw_entries(view)
    start_min, end_min = _window([raw])
    return Timeline(
        hours=list(range(start_min // 60, end_min // 60 + 1)),
        height=(end_min - start_min) * PX_PER_MIN,
        entries=_place(raw, start_min, px=PX_PER_MIN, min_height=14),
        now_top=_now_top(view, today, start_min, end_min, PX_PER_MIN),
    )


# ── the headline strip: what is happening, what free time is left ─────────
#
# The strip answers the two questions asked at a glance — "what am I in right now"
# and "does the day fit" — from the same entries the canvas below draws, so the two
# can never disagree. Capacity stays the docs/04 §4.3 sentence; the only chip on the
# page is the gold overflow warning. No verdict chips: §4 keeps green for done and
# black for due-today, and a FITS badge would spend both on a mood.


@dataclass(frozen=True)
class NowNext:
    state: str  # NOW | NEXT
    title: str
    detail: str  # 'until 10:30' for NOW, the start time for NEXT


@dataclass(frozen=True)
class Gap:
    start_label: str
    end_label: str
    minutes: int

    @property
    def duration(self) -> str:
        h, m = divmod(self.minutes, 60)
        return f"{h}h {m:02d}m" if h else f"{m}m"


@dataclass(frozen=True)
class DayNotes:
    """The two planner sentences the day page owes the owner, re-derived from what
    was persisted (day_plan carries no notes column). P3 is capacity under the
    configured floor; P9 is a planned day that got no protected block — the sentence
    docs/04 calls the product, so it renders as visible body text, never folded."""

    fully_booked: bool
    fragmented: bool


def now_next(view: DayView, *, today: date) -> NowNext | None:
    """Today only: the block containing the current minute, else the next one to start."""
    if view.day != today:
        return None
    now_min = _now_minute(view)
    raw = _raw_entries(view)
    for start, dur, title, _kind, _outcome, _travel in raw:
        if start <= now_min < start + dur:
            return NowNext("NOW", title, f"until {_clock(start + dur)}")
    for start, _dur, title, _kind, _outcome, _travel in raw:
        if start > now_min:
            return NowNext("NEXT", title, _clock(start))
    return None


#: Anything shorter is a seam between back-to-back blocks, not usable free time.
MIN_GAP_MINUTES = 15


def gaps(view: DayView) -> list[Gap]:
    """Free intervals inside the day's ruler — P9 fragmentation made countable."""
    raw = _raw_entries(view)
    if not raw:
        return []
    start_min, end_min = _window([raw])
    out: list[Gap] = []
    cursor = start_min
    for start, dur, _title, _kind, _outcome, _travel in raw:
        if start - cursor >= MIN_GAP_MINUTES:
            out.append(Gap(_clock(cursor), _clock(start), start - cursor))
        cursor = max(cursor, start + dur)
    if end_min - cursor >= MIN_GAP_MINUTES:
        out.append(Gap(_clock(cursor), _clock(end_min), end_min - cursor))
    return out


def day_notes(view: DayView, settings: Settings) -> DayNotes:
    if not view.blocks:
        return DayNotes(fully_booked=False, fragmented=False)
    capacity_minutes = int(view.blocks[0]["capacity_minutes"])
    fully_booked = capacity_minutes < settings.min_capacity_minutes
    fragmented = not fully_booked and not any(b["kind"] == "protected" for b in view.blocks)
    return DayNotes(fully_booked=fully_booked, fragmented=fragmented)


# ── the week agenda grid ──────────────────────────────────────────────────
#
# Seven proportional mini-timelines sharing one hour ruler — the all-days-one-view
# agenda pattern. Half a pixel per minute keeps a 15-hour window near 450px, and the
# shared window is what makes 10:00 sit at the same height in every column.

WEEK_PX = 0.5
#: At 0.5px/min a 25-minute block is 12px: one 10px text line still fits.
WEEK_MIN_HEIGHT = 12


@dataclass(frozen=True)
class WeekCol:
    view: DayView
    entries: list[Entry]
    now_top: int | None
    cap: dict[str, Any] | None

    @property
    def free_minutes(self) -> int | None:
        """The capacity band's headline number. None means the day has no plan at
        all — the band renders an em-dash there rather than a fake zero."""
        if not self.cap:
            return None
        return max(int(self.cap["capacity_minutes"]) - int(self.cap["planned_minutes"]), 0)


@dataclass(frozen=True)
class WeekTimeline:
    hours: list[int]
    height: int
    hour_px: int
    cols: list[WeekCol]

    @property
    def planned_minutes(self) -> int:
        return sum(int(c.cap["planned_minutes"]) for c in self.cols if c.cap)

    @property
    def capacity_minutes(self) -> int:
        return sum(int(c.cap["capacity_minutes"]) for c in self.cols if c.cap)

    @property
    def verdict(self) -> str | None:
        """G5 where the week is visible — and only when it says something. An
        unremarkable week has no line at all (docs/04 §4: sections vanish)."""
        if self.planned_minutes <= self.capacity_minutes:
            return None
        return (
            f"{self.planned_minutes // 60}h planned against "
            f"{self.capacity_minutes // 60}h available this week"
        )


def week_timeline(views: list[DayView], *, today: date) -> WeekTimeline:
    raws = [_raw_entries(v) for v in views]
    start_min, end_min = _window(raws)
    cols = [
        WeekCol(
            view=v,
            entries=_place(raw, start_min, px=WEEK_PX, min_height=WEEK_MIN_HEIGHT),
            now_top=_now_top(v, today, start_min, end_min, WEEK_PX),
            # Day-level capacity fields ride on every block row of dashboard_today.
            cap=v.blocks[0] if v.blocks else None,
        )
        for v, raw in zip(views, raws, strict=True)
    ]
    return WeekTimeline(
        hours=list(range(start_min // 60, end_min // 60 + 1)),
        height=round((end_min - start_min) * WEEK_PX),
        hour_px=round(60 * WEEK_PX),
        cols=cols,
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
        date_: DayParam | None = Query(None, alias="date"),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        # Typed `date`, not `str` parsed in the body: a hand-edited URL, a stale
        # bookmark or a typo used to reach `date.fromisoformat` unguarded and 500 the
        # page. FastAPI answers the same input with a 422 naming the parameter, which
        # is what /brief/{on_date} has always done by virtue of typing its parameter.
        day = date_ or today()
        view = day_view(conn, settings, day)
        return templates.TemplateResponse(
            request,
            "schedule.html",
            {
                "view": view,
                "tl": timeline(view, today=today()),
                "cap": view.blocks[0] if view.blocks else None,
                "now_next": now_next(view, today=today()),
                "gaps": gaps(view),
                "notes": day_notes(view, settings),
                "prev": (day - timedelta(days=1)).isoformat(),
                "next": (day + timedelta(days=1)).isoformat(),
                "week_start": week_of(day).isoformat(),
                "settings": settings,
            },
        )

    @router.get("/schedule/week", response_class=HTMLResponse)
    def week(
        request: Request,
        start: DayParam | None = Query(None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        first = week_of(start or today())
        days = [day_view(conn, settings, first + timedelta(days=i)) for i in range(7)]
        return templates.TemplateResponse(
            request,
            "schedule_week.html",
            {
                "wtl": week_timeline(days, today=today()),
                "first": first,
                "prev": (first - timedelta(days=7)).isoformat(),
                "next": (first + timedelta(days=7)).isoformat(),
                "today": today(),
                "settings": settings,
            },
        )

    return router
