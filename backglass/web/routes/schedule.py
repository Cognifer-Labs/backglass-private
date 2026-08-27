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
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import date, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from annotated_types import Ge, Le
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from backglass.config import Settings
from backglass.db import query
from backglass.ledger import USER_ID
from backglass.plan import capacity, planner, priority, timezones
from backglass.web import actions


@dataclass(frozen=True)
class DayView:
    day: date
    tz: str
    blocks: list[dict[str, Any]]
    fixed: list[capacity.FixedEvent]

    @property
    def empty(self) -> bool:
        return not self.blocks and not self.fixed

    @property
    def unplanned(self) -> bool:
        """No planner has visited this day. Routines keep the canvas alive, so this —
        not `empty` — is what decides whether to name the planner command."""
        return not self.blocks


def day_view(conn: sqlite3.Connection, settings: Settings, day: date) -> DayView:
    tz = timezones.active_tz(settings, day)
    blocks = [
        dict(row)
        for row in conn.execute(
            query("dashboard_today"),
            {"user_id": USER_ID, "local_date": day.isoformat()},
        )
    ]
    # The whole day's fixed picture — calendar, confirmed plans, routines — not just
    # the calendar reader. This is what puts a 7:15pm dinner and a 7:30am breakfast on
    # the canvas for a day no planner has visited; `_collapse` merges the copies for a
    # day one has.
    return DayView(
        day=day, tz=tz, blocks=blocks, fixed=capacity.day_events(conn, settings, day)
    )


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
    #: How many lanes the overlap cluster this entry belongs to was split into. The
    #: pair is what the stylesheet needs: `lane` is which column, `lanes` is how wide
    #: a column is. Carried per entry rather than per timeline so a day with one
    #: triple-booked hour does not narrow every other block on the canvas to a third.
    lanes: int = 1
    outcome: str = ""
    travel: bool = False
    #: Carried through from `RawEntry` so the canvas can act on what it draws, and
    #: `None` where there is genuinely nothing to act on. A routine is configuration
    #: with no row behind it and gets no buttons rather than a button that lies.
    block_id: int | None = None
    source_item_id: int | None = None
    pinned: bool = False

    @property
    def actionable(self) -> bool:
        return self.block_id is not None or self.source_item_id is not None

    @property
    def slim(self) -> bool:
        """Under 46px the three text lines cannot fit; render one compressed line."""
        return self.height < 46

    @property
    def tiny(self) -> bool:
        """Under 20px even slim's single 13px line plus keylines does not fit. Rather
        than shrink type below the 11px floor, the title is deleted from the canvas
        and moved to the title attribute: stripe plus start time is all that renders.
        The threshold sat at 32px until the routines landed: a 25–30 minute block
        (breakfast, a short reply) is the common case, slim's one row fits inside
        20px, and a canvas of unnamed boxes is a schedule that cannot be read."""
        return self.height < 20


@dataclass(frozen=True)
class Timeline:
    hours: list[int]
    height: int
    entries: list[Entry]
    now_top: int | None
    #: The window the canvas draws, in minutes of the day. Carried rather than derived
    #: from `hours[0] * 60`: `_window` is not hour-aligned, and `now.js` moves the Now
    #: line by re-doing `_now_top`'s arithmetic in the browser — off by the same amount
    #: the ruler is, or off by nothing.
    start_min: int = 0
    end_min: int = 24 * 60
    #: Whether this is the day the Now line belongs on. `now_top` is None both when the
    #: day is not today and when the clock is outside the drawn window, and the browser
    #: has to tell those apart: the second one starts moving at 7:30am, the first never
    #: does.
    is_today: bool = False


def _minutes(hhmm: str) -> int | None:
    """`HH:MM` as a minute of the day, or None if that is not what this is.

    Callers slice it out of a stored timestamp by position (`starts_at[11:16]`), which
    is right for every stamp the planner writes and is not a guarantee: `plan_block.
    starts_at` is TEXT with no CHECK behind it, and one row that is not a full ISO
    timestamp used to raise `ValueError: invalid literal for int()` out of the template
    call — taking down the whole day page and, because the week grid builds from the
    same reader, all seven days with it. Rule 5's unit here is the block: one row the
    page cannot place is one row missing from the ruler, not a blank screen.
    """
    try:
        return int(hhmm[:2]) * 60 + int(hhmm[3:5])
    except ValueError:
        return None


def _clock(minute_of_day: int) -> str:
    return timezones.clock12(minute_of_day)


@dataclass(frozen=True)
class RawEntry:
    """One thing on the day, in the minute coordinates this page works in.

    A six-tuple until 2026-08-27, and it had to stop being one the moment the entries
    started carrying identity. Five call sites unpack it, `_clusters` and `_window`
    index into it, and the two new fields are both `int | None` sitting next to each
    other — the exact shape that makes a positional slip type-check and draw the wrong
    button on the wrong block.
    """

    start: int
    minutes: int
    title: str
    kind: str
    outcome: str = ""
    travel: bool = False
    #: The `plan_block` row, when a planner wrote one. What Done/Roll/Pin act on.
    block_id: int | None = None
    #: Whether that block is already held against the replanner. Carried so the button
    #: can be a toggle rather than a one-way trip — the same Pin/Unpin the Today panel
    #: has always drawn.
    pinned: bool = False
    #: The `source_item` this was read out of, when a calendar reader supplied it.
    #: What "not happening" retracts, and what /source/{id} shows the evidence for.
    source_item_id: int | None = None

    @property
    def end(self) -> int:
        return self.start + self.minutes


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

    The two ids merge under that same rule, and they are the reason it matters most.
    Only the plan copy knows its `block_id`; only the calendar copy knows the
    `source_item_id` behind it. One event, two doors — Done/Roll/Pin act on the plan
    row, "not happening" retracts the calendar row — and a merge that kept one copy
    whole would take away whichever door the loser held. Before 2026-08-27 this function
    dropped both, because there was nothing to drop: the entries had no identity at all,
    and correcting a wrong class time meant writing a script.
    """
    kept: dict[tuple[int, int, str], RawEntry] = {}
    for entry in raw:
        identity = (entry.start, entry.minutes, entry.title.casefold())
        seen = kept.get(identity)
        if seen is None:
            kept[identity] = entry
            continue
        kept[identity] = replace(
            seen,
            outcome=seen.outcome or entry.outcome,
            travel=seen.travel or entry.travel,
            block_id=seen.block_id or entry.block_id,
            pinned=seen.pinned or entry.pinned,
            source_item_id=seen.source_item_id or entry.source_item_id,
        )
    return list(kept.values())


def allday(view: DayView) -> list[str]:
    """The day's all-day banners, as titles — plans that named a day and no hour.

    Kept off the timeline on purpose. They span midnight to midnight, so drawing one
    would paint over every real block on the canvas and stretch the shared week ruler to
    24 hours; and the whole reason they are all-day is that nobody said when. A banner
    states the fact without asserting an hour.
    """
    seen: dict[str, None] = {}
    for e in view.fixed:
        if e.allday:
            seen.setdefault(e.title or "Busy", None)
    for b in view.blocks:
        if str(b["kind"]) == "allday":
            seen.setdefault(str(b["title"]), None)
    return list(seen)


def _raw_entries(view: DayView) -> list[RawEntry]:
    raw: list[RawEntry] = []
    for e in view.fixed:
        if e.allday:
            continue  # a banner, not an hour — see `allday` above
        raw.append(
            RawEntry(
                start=e.starts_at.hour * 60 + e.starts_at.minute,
                minutes=max(e.minutes, 1),
                title=e.title or "Busy",
                kind=e.kind,
                travel=e.travel,
                source_item_id=e.source_item_id,
            )
        )
    for b in view.blocks:
        if str(b["kind"]) == "allday":
            continue
        if str(b["outcome"]) == "dropped":
            # "Not this slot" — `actions.set_block_outcome`'s own words. A block the
            # owner has taken off the day must stop holding one, or `gaps` reports free
            # time that is not free and the canvas keeps asserting a class that was
            # cancelled. Done and rolled still draw: those happened, and the day is
            # also a record of itself.
            continue
        start = _minutes(str(b["starts_at"])[11:16])
        end = _minutes(str(b["ends_at"])[11:16])
        if start is None or end is None:
            # Dropped, not placed. The page draws nothing but this timeline, so a row
            # skipped here is invisible — which is the right trade against the two
            # alternatives: raising takes the whole day (and the week's other six days)
            # down, and defaulting to midnight draws a block at a time nothing says it
            # happens. A schedule that asserts a wrong hour is worse than one missing a
            # row it could not read.
            continue
        raw.append(
            RawEntry(
                start=start,
                minutes=max(end - start, 1),
                title=str(b["title"]),
                kind=str(b["kind"]),
                outcome=str(b["outcome"]),
                block_id=int(b["id"]),
                pinned=bool(b["pinned"]),
            )
        )
    raw = _collapse(raw)
    raw.sort(key=lambda r: (r.start, -r.minutes))
    return raw


def _window(raws: list[list[RawEntry]]) -> tuple[int, int]:
    """Hour-snapped span covering the default working window plus anything
    scheduled outside it — across every day given, so week columns share a ruler."""
    flat = [r for raw in raws for r in raw]
    start_min = min([WINDOW_START_H * 60, *(r.start for r in flat)])
    end_min = max([WINDOW_END_H * 60, *(r.end for r in flat)])
    return (start_min // 60) * 60, ((end_min + 59) // 60) * 60


def _clusters(raw: list[RawEntry]) -> list[list[RawEntry]]:
    """`raw`, already sorted by start, cut into runs of mutually overlapping entries.

    A cluster ends at the first entry that starts at or after everything before it has
    finished — the running maximum end, not the previous entry's end, because a two-hour
    dinner can span three shorter blocks that each end before the next begins.

    Clustering is what keeps the columns local. Lanes are a property of a collision, so
    a day holding one triple-booked evening and nine ordinary blocks should draw nine
    full-width blocks and three thirds, not twelve thirds.
    """
    out: list[list[RawEntry]] = []
    reach: int | None = None
    for entry in raw:
        if reach is None or entry.start >= reach:
            out.append([])
            reach = entry.end
        else:
            reach = max(reach, entry.end)
        out[-1].append(entry)
    return out


def _place(raw: list[RawEntry], start_min: int, *, px: float, min_height: int) -> list[Entry]:
    """Entries positioned, and side by side wherever P5's ban on overlap does not hold.

    P5 bans the planner from overlapping its own blocks, and nothing bans the day itself:
    fixed events collide with each other and with the routines, which have no capacity
    negotiation at all — they are anchors, drawn where the owner said they happen. The
    evening of 2026-08-09 held five at once (gym, two versions of the same McKenna dinner,
    shower, dinner), and the two-lane packing here put three of them in lane 1 without
    ever asking whether lane 1 was free: `start >= lane_ends[0]` decides lane 0, and the
    else branch was lane 1 unconditionally. Drawn, they stacked, and the titles underneath
    were unreadable — a canvas asserting a schedule nobody could check.

    So: first fit across as many lanes as the cluster needs. The first lane whose last
    entry has finished takes it; if none has, the cluster grows a column. Entry order is
    unchanged — clusters come out in start order and each preserves its own — so the
    document order the templates rely on is the same one `_raw_entries` sorted.
    """
    entries: list[Entry] = []
    for cluster in _clusters(raw):
        lane_ends: list[int] = []
        placed: list[tuple[int, RawEntry]] = []
        for entry in cluster:
            lane = next(
                (i for i, end in enumerate(lane_ends) if entry.start >= end),
                len(lane_ends),
            )
            if lane == len(lane_ends):
                lane_ends.append(0)
            lane_ends[lane] = entry.end
            placed.append((lane, entry))
        for lane, entry in placed:
            entries.append(
                Entry(
                    title=entry.title,
                    kind=entry.kind,
                    start_label=_clock(entry.start),
                    end_label=_clock(entry.end),
                    top=round((entry.start - start_min) * px),
                    height=max(round(entry.minutes * px), min_height),
                    lane=lane,
                    lanes=len(lane_ends),
                    outcome=entry.outcome,
                    travel=entry.travel,
                    block_id=entry.block_id,
                    source_item_id=entry.source_item_id,
                    pinned=entry.pinned,
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
        start_min=start_min,
        end_min=end_min,
        is_today=view.day == today,
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
    #: A day with no working window at all, which reaches zero capacity by a different
    #: road than P3's. Kept apart from `fully_booked` because the two sentences send the
    #: owner in opposite directions — one to decline a meeting, the other to notice that
    #: Saturday is not in `working_days`.
    off_day: bool = False


def now_next(view: DayView, *, today: date) -> NowNext | None:
    """Today only: the block containing the current minute, else the next one to start."""
    if view.day != today:
        return None
    now_min = _now_minute(view)
    raw = _raw_entries(view)
    for entry in raw:
        if entry.start <= now_min < entry.end:
            return NowNext("NOW", entry.title, f"until {_clock(entry.end)}")
    for entry in raw:
        if entry.start > now_min:
            return NowNext("NEXT", entry.title, _clock(entry.start))
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
    for entry in raw:
        if entry.start - cursor >= MIN_GAP_MINUTES:
            out.append(Gap(_clock(cursor), _clock(entry.start), entry.start - cursor))
        cursor = max(cursor, entry.end)
    if end_min - cursor >= MIN_GAP_MINUTES:
        out.append(Gap(_clock(cursor), _clock(end_min), end_min - cursor))
    return out


def day_notes(view: DayView, settings: Settings) -> DayNotes:
    if not view.blocks:
        return DayNotes(fully_booked=False, fragmented=False)
    capacity_minutes = int(view.blocks[0]["capacity_minutes"])
    under_floor = capacity_minutes < settings.min_capacity_minutes
    # `day_plan` stores capacity but not the window it came from, so the page asks the
    # configuration the same question `compute` did rather than inferring from the zero.
    off_day = under_floor and not timezones.is_working_day(settings, view.day)
    fully_booked = under_floor and not off_day
    fragmented = not under_floor and not any(b["kind"] == "protected" for b in view.blocks)
    return DayNotes(fully_booked=fully_booked, fragmented=fragmented, off_day=off_day)


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
    #: Titles of the day's all-day plans. They are not entries — nothing on the ruler
    #: can express "all day" — so the column carries them beside it and the grid draws
    #: them above its own hours.
    allday: list[str] = dataclass_field(default_factory=list)

    @property
    def busy(self) -> bool:
        """Something beyond the routine template is on this day. Routines repeat on
        all seven columns by construction, so they alone cannot earn the grid its
        ink — an empty week stays a sentence, not a framed void of breakfasts.

        An all-day plan counts. A day whose only non-routine content is a six-day
        programme has no entries at all, so without this the week reads as quiet and
        can collapse to the "nothing on" sentence while the owner is at the programme."""
        return bool(self.allday) or any(e.kind != "routine" for e in self.entries)

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
    #: The drawn window, for the same reason `Timeline` carries it — `now.js` moves this
    #: grid's Now line too, at half a pixel a minute instead of one.
    start_min: int = 0
    end_min: int = 24 * 60

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
            allday=allday(v),
        )
        for v, raw in zip(views, raws, strict=True)
    ]
    return WeekTimeline(
        hours=list(range(start_min // 60, end_min // 60 + 1)),
        height=round((end_min - start_min) * WEEK_PX),
        hour_px=round(60 * WEEK_PX),
        cols=cols,
        start_min=start_min,
        end_min=end_min,
    )


def _act(fn: Callable[..., Any], *args: Any) -> None:
    """`app.py::_run`, for the routes that live here.

    Not imported from there: `app.py` imports this module to build the router, so
    reaching back for it would close the cycle. It is four lines, and the alternative is
    a shared module holding one function.
    """
    try:
        fn(*args)
    except actions.ActionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _describe(
    conn: sqlite3.Connection, day: date, source_item_id: int
) -> dict[str, Any] | None:
    """What a calendar event was, in the words the undo strip needs.

    Read before the retraction, because afterwards every reader on this page filters the
    row out by design and the strip would have nothing to name.
    """
    row = conn.execute(
        "SELECT title, occurred_at FROM source_item WHERE id = ? AND user_id = ?",
        (source_item_id, USER_ID),
    ).fetchone()
    if row is None:
        return None
    minute = _minutes(str(row["occurred_at"])[11:16])
    return {
        "source_item_id": source_item_id,
        "title": str(row["title"] or "Busy"),
        "when": _clock(minute) if minute is not None else day.isoformat(),
    }


@dataclass(frozen=True)
class RunwayRow:
    """One obligation on the runway, in the words the page prints.

    Flattened here rather than in the template because two of these fields are
    judgements — whether a thing is late, and whether it fits at all — and a judgement
    made inside a Jinja expression is one nobody can test.
    """

    due: str
    what: str
    #: "Mon 31 · 40m" per sitting, already ordered.
    spread: list[str]
    minutes: int
    overdue: bool
    #: The work does not finish before it is owed, at the capacity of the horizon. The
    #: whole reason this panel is worth opening.
    unreachable: bool = False

    @property
    def days(self) -> int:
        return len(self.spread)


def _runway_rows(
    pool: list[Any], horizon: Any, *, today_: date | None = None
) -> list[RunwayRow]:
    """The runway as rows, deadline first, unreachable work at the top.

    Unreachable first because it is the only part that asks for a decision. Everything
    below it is the plan working; the top of the list is the plan telling you it cannot.
    """
    by_id = {c.commitment_id: c for c in pool}
    start = today_ or horizon.start
    unreachable_ids = {c.commitment_id for c in horizon.unreachable}
    rows: list[RunwayRow] = []

    def _due_key(item: Any) -> str:
        return str(item.due_at or "9999")[:10]

    def _row(item: Any, sittings: list[Any], unreachable: bool) -> RunwayRow:
        due = str(item.due_at)[:10] if item.due_at else ""
        return RunwayRow(
            due=due,
            what=item.what,
            spread=[f"{s.day.strftime('%a %d')} · {s.minutes}m" for s in sittings],
            minutes=item.remaining,
            overdue=bool(due and due < start.isoformat()),
            unreachable=unreachable,
        )

    for item in sorted(horizon.unreachable, key=_due_key):
        rows.append(_row(item, horizon.sittings.get(item.commitment_id, []), True))
    allocated = sorted(
        (cid for cid in horizon.sittings if cid not in unreachable_ids),
        key=lambda cid: (_due_key(by_id[cid]), cid),
    )
    rows.extend(_row(by_id[cid], horizon.sittings[cid], False) for cid in allocated)
    return rows


def _beyond(horizon: Any) -> dict[str, Any] | None:
    """The work the fortnight never reached, as one sentence's worth of facts.

    A count and a total rather than a hundred rows: the panel is already 137 lines of
    obligations that *do* have days, and appending every October assignment with an
    em-dash where its days go would bury the part that is advice. What the owner needs
    from this is that the list is a window, not the board — and how much of the board is
    outside it. `None` when everything fitted, so the sentence disappears with the fact.
    """
    if not horizon.beyond:
        return None
    minutes = sum(u.minutes for u in horizon.beyond)
    return {
        "count": len(horizon.beyond),
        "hours": minutes // 60,
        # EDF order, so the first one out is the earliest deadline the fortnight missed.
        "earliest": str(horizon.beyond[0].item.due_at)[:10],
    }


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
        plan_row = conn.execute(
            "SELECT id, status FROM day_plan WHERE user_id = ? AND local_date = ?"
            " AND status != 'superseded' ORDER BY id DESC LIMIT 1",
            (USER_ID, day.isoformat()),
        ).fetchone()
        return templates.TemplateResponse(
            request,
            "schedule.html",
            {
                "plan": dict(plan_row) if plan_row else None,
                "view": view,
                "tl": timeline(view, today=today()),
                "cap": view.blocks[0] if view.blocks else None,
                "now_next": now_next(view, today=today()),
                "gaps": gaps(view),
                "allday": allday(view),
                "notes": day_notes(view, settings),
                # The conflict priority list, stated on the page that shows the day it
                # governs. Owner's ruling 2026-08-24: a rule nobody can read is a rule
                # nobody can disagree with, and this one had been distributed across four
                # files and written down nowhere.
                "priority_tiers": priority.TIERS,
                "prev": (day - timedelta(days=1)).isoformat(),
                "next": (day + timedelta(days=1)).isoformat(),
                "week_start": week_of(day).isoformat(),
                "settings": settings,
            },
        )

    @router.get("/schedule/runway", response_class=HTMLResponse)
    def runway_view(
        request: Request,
        date_: DayParam | None = Query(None, alias="date"),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """The fortnight behind the day, as a fragment the fold loads on first open.

        Its own route rather than part of the page, because `runway.allocate` computes
        fourteen days of capacity and measured 0.58s on the owner's ledger — a real cost
        on a page opened every morning, and one nobody should pay to look at today. The
        fold is closed by default and asks for this once (`hx-trigger="toggle once"`), so
        the day page stays as fast as it was and the runway costs only the mornings
        somebody wants it.
        """
        from backglass.goals import health
        from backglass.plan import runway as runway_mod

        day = date_ or today()
        pool = planner.candidates(
            conn, settings, day, health.at_risk_goal_ids(conn, settings, day)
        )
        horizon = runway_mod.allocate(conn, settings, pool, day)
        return templates.TemplateResponse(
            request,
            "_runway.html",
            {
                "rows": _runway_rows(pool, horizon),
                "horizon_days": runway_mod.DEFAULT_HORIZON_DAYS,
                "unreadable": horizon.unreadable,
                "beyond": _beyond(horizon),
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

    # ── acting on what the canvas draws ──────────────────────────────────
    #
    # Owner's ask, 2026-08-27: "everything in schedule should be clickable and
    # interactable" — said in the same breath as "chem lab is no longer 6 to 7:50
    # Thursday, why hasn't backglass backend updated to show that". The two are one
    # request. The lab moved on the 23rd, the ledger knew within hours, and the day page
    # drew the old hour for four more days because the only door to a wrong calendar row
    # was a Python script.
    #
    # These re-render the timeline rather than redirecting, so acting on a block does not
    # throw away the scroll position on a canvas that is often taller than the window.
    # They are scoped under /schedule/{on_date}/ rather than reusing the flat /blocks/…
    # routes for one reason: those return the Today panel, and a fragment swap has to
    # answer with the fragment that was asked for.

    def _timeline_fragment(
        request: Request,
        conn: sqlite3.Connection,
        day: date,
        undo: dict[str, Any] | None = None,
    ) -> Any:
        view = day_view(conn, settings, day)
        return templates.TemplateResponse(
            request,
            "_timeline.html",
            {"view": view, "tl": timeline(view, today=today()), "undo": undo},
        )

    def _mirror_block(
        conn: sqlite3.Connection, day: date, source_item_id: int
    ) -> int | None:
        """The `plan_block` the planner projected from this calendar event, if any.

        `plan/planner` persists a `kind='fixed'` block for every event in `cap.fixed`, so
        retracting the calendar row leaves the planner's copy standing on the day — which
        reads, correctly, as the button having done nothing. The complaint this page is
        answering is a wrong class time surviving a correction, so a half-applied
        correction is the one outcome worth spending code to prevent.

        Identity is `_collapse`'s, asked rather than re-derived: the merge that decides
        two copies are one event is the only place that judgement should live, and a
        `plan_block` carries no `source_item_id` to join on. Read before the retraction,
        while the calendar copy is still on the canvas to be merged with.
        """
        view = day_view(conn, settings, day)
        for entry in _raw_entries(view):
            if entry.source_item_id == source_item_id:
                return entry.block_id
        return None

    @router.post("/schedule/{on_date}/blocks/{block_id}/outcome/{outcome}",
                 response_class=HTMLResponse)
    def block_outcome(
        on_date: DayParam,
        block_id: int,
        outcome: str,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        _act(actions.set_block_outcome, conn, block_id, outcome)
        return _timeline_fragment(request, conn, on_date)

    @router.post("/schedule/{on_date}/blocks/{block_id}/pin/{pinned}",
                 response_class=HTMLResponse)
    def block_pin(
        on_date: DayParam,
        block_id: int,
        pinned: int,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        _act(actions.pin_block, conn, block_id, bool(pinned))
        return _timeline_fragment(request, conn, on_date)

    @router.post("/schedule/{on_date}/source/{source_item_id}/retract",
                 response_class=HTMLResponse)
    def retract_event(
        on_date: DayParam,
        source_item_id: int,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Take a calendar event off the day. The door `retraction.py` cannot open.

        That module only ever infers a retraction from a window a connector re-read and
        certified complete — the safety property it is built around. The rows behind the
        owner's CHM 113 lab are a one-shot registrar import that no connector re-reads,
        so no certified read will ever cover them and the owner's own eyes are the only
        evidence there will be.

        The event is described back before it is removed, because it is about to vanish
        from the canvas and the undo cannot live on a block that is no longer drawn.
        """
        described = _describe(conn, on_date, source_item_id)
        mirror = _mirror_block(conn, on_date, source_item_id)
        _act(actions.retract_source_item, conn, source_item_id)
        if mirror is not None:
            _act(actions.set_block_outcome, conn, mirror, "dropped")
            if described is not None:
                described["block_id"] = mirror
        return _timeline_fragment(request, conn, on_date, undo=described)

    @router.post("/schedule/{on_date}/source/{source_item_id}/restore",
                 response_class=HTMLResponse)
    def restore_event(
        on_date: DayParam,
        source_item_id: int,
        request: Request,
        block: int | None = Query(None),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Undo, both halves. The retraction put the calendar row back out of reach and
        dropped the plan block mirroring it; leaving the block dropped would undo half
        the action and leave the day quietly short of a class."""
        _act(actions.restore_source_item, conn, source_item_id)
        if block is not None:
            _act(actions.set_block_outcome, conn, block, "pending")
        return _timeline_fragment(request, conn, on_date)

    @router.post("/schedule/{on_date}/accept")
    def accept_plan(
        on_date: DayParam, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Any:
        """The owner takes the proposed plan. From here the replanner may only knock
        (plan/replan.py's boundary) — which is exactly why this needs a button: the
        boundary is meaningless if accepting requires a terminal."""
        from backglass.db import now_iso

        conn.execute(
            "UPDATE day_plan SET status = 'accepted', accepted_at = ?"
            " WHERE user_id = ? AND local_date = ? AND status = 'proposed'",
            (now_iso(), USER_ID, on_date.isoformat()),
        )
        return RedirectResponse(f"/schedule?date={on_date.isoformat()}", status_code=303)

    @router.post("/schedule/{on_date}/replan")
    def replan_day(
        on_date: DayParam, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Any:
        """Rebuild the day from the current world — the owner's own click, so it may
        replace even an accepted plan (superseding, never deleting; docs/04 §3). This
        is the door the plan-drift knock points at."""
        from backglass.goals import health
        from backglass.plan import planner, timezones

        now = timezones.local_now(settings)
        proposal = planner.propose(
            conn,
            settings,
            on_date,
            at_risk_goals=health.at_risk_goal_ids(conn, settings, on_date),
            now=now,
        )
        planner.persist(conn, settings, proposal)
        return RedirectResponse(f"/schedule?date={on_date.isoformat()}", status_code=303)

    return router
