"""The capacity model. docs/04 §1.2.

    "The single most common failure of automated day planning is proposing eight hours of
    work into a day that has five hours of meetings. So capacity is computed first, and it
    is a hard constraint rather than a display value."

    working_window   = configured per weekday (default 09:00–18:00 local)
    fixed            = calendar events marked busy, minus declined
    buffer           = 10 min after any meeting >= 30 min, 5 min otherwise
    commute/travel   = any calendar event tagged travel, plus its buffer
    capacity_minutes = working_window − fixed − buffer − travel − reserve − reviews
    reserve          = configured slack, default 45 min/day, never zero
    reviews          = today's spaced-repetition load, when a due
                       snapshot exists; zero otherwise

The reserve is the requirement most likely to be "optimised" away by someone trying to fit
one more thing in. docs/04: "A plan that fills every minute is a plan that fails at 10:15
and stays failed." `reserve_minutes` is clamped to at least one minute for that reason.

Fixed events come from `source_item` rows written by the calendar connector, which is
Phase 5. Until it exists this returns an empty list and capacity is the whole window minus
the reserve — which is correct, not a stub: a day with no known meetings genuinely has no
known meetings.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from backglass.config import Settings, parse_routines
from backglass.ledger import USER_ID
from backglass.plan import timezones

#: docs/07 §Calendar: "Declined events are excluded from capacity. Tentative events count
#: as busy." Tentative counting as busy is the conservative direction — under-promising
#: capacity costs one unscheduled hour, over-promising costs a missed commitment.
BUSY_STATUSES = {"confirmed", "tentative", "busy"}

#: Ceiling on the capacity reserved for spaced-repetition reviews. Two
#: hours a day is a heavy but real review load; anything above it means a backlog
#: the owner should triage deliberately rather than have the planner silently
#: surrender the day to.
REVIEW_CAP_MINUTES = 120

#: How long a confirmed plan is assumed to run when the message never said. An hour is
#: the honest floor for "dinner at seven": people rarely state an end time for social
#: arrangements, and assuming a shorter one would hand the planner minutes the owner does
#: not have. It is only ever a fallback — a stated `ends_at` always wins.
DEFAULT_ENGAGEMENT = timedelta(minutes=60)


@dataclass(frozen=True)
class FixedEvent:
    starts_at: datetime
    ends_at: datetime
    title: str = ""
    travel: bool = False
    #: `fixed` for calendar events and confirmed plans; `routine` for the configured
    #: daily anchors (breakfast, gym); `allday` for a confirmed plan that named a day
    #: and no hour. The distinction matters three times: routines get no meeting buffer,
    #: an all-day banner spends no capacity at all, and the schedule draws each in its
    #: own register.
    kind: str = "fixed"

    @property
    def allday(self) -> bool:
        """Spans a day rather than occupying an hour of it.

        Kept as a property rather than as a second flag beside `kind` so there is one
        place to be wrong. Everything that subtracts time checks this; nothing else has
        to know the string.
        """
        return self.kind == "allday"

    @property
    def minutes(self) -> int:
        return max(0, int((self.ends_at - self.starts_at).total_seconds() // 60))


@dataclass
class Slot:
    """A contiguous stretch of the working window with nothing fixed in it."""

    starts_at: datetime
    ends_at: datetime

    @property
    def minutes(self) -> int:
        return max(0, int((self.ends_at - self.starts_at).total_seconds() // 60))


@dataclass
class Capacity:
    day: date
    tz: str
    window_minutes: int
    fixed_minutes: int
    buffer_minutes: int
    travel_minutes: int
    reserve_minutes: int
    capacity_minutes: int
    #: Today's spaced-repetition load: due cards × the owner's own
    #: trailing pace. Subtracted like the reserve — reviews happen whether or not
    #: they are scheduled, so a plan that ignores them over-promises the day.
    review_minutes: int = 0
    slots: list[Slot] = field(default_factory=list)
    fixed: list[FixedEvent] = field(default_factory=list)

    @property
    def plannable(self) -> bool:
        """P3. "If capacity is under 60 minutes, do not propose a plan."."""
        return self.capacity_minutes >= self._min_capacity

    @property
    def no_window(self) -> bool:
        """True when the day has no working window at all, rather than a consumed one.

        Both end at zero capacity and they are opposite facts. "Fully booked" tells the
        owner their meetings ate the day and the fix is to decline one; a day that is
        simply not in `working_days` is not booked at all, and telling them it is booked
        sends them looking for meetings that do not exist. Derived rather than stored:
        the non-working branch of `compute` is the only one that returns a zero window,
        so the distinction cannot drift out of sync with the thing it describes.
        """
        return self.window_minutes == 0

    _min_capacity: int = 60

    def longest_slot(self) -> Slot | None:
        return max(self.slots, key=lambda s: s.minutes) if self.slots else None


def fixed_events(conn: sqlite3.Connection, day: date, tz: str) -> list[FixedEvent]:
    """Read calendar events for `day` out of the ledger.

    The calendar connector writes them as ordinary `source_item` rows with the
    event-specific fields in `raw_json` — docs/03: "If a field only makes sense for one
    source, it belongs in raw_json, not in a column." So the capacity model needs no new
    table and no schema change when the connector lands in Phase 5.
    """
    # Instant comparison, not date(): the connector stores each event's own offset, and
    # SQLite's date() normalizes to UTC first — which silently dropped every Phoenix
    # event after ~17:00 from its own day, so the planner scheduled work across it.
    # timezones.utc_bounds carries the full reasoning.
    starts_at, ends_before = timezones.day_bounds(day, tz)
    rows = conn.execute(
        "SELECT raw_json, title FROM source_item "
        "WHERE user_id = ? AND source LIKE 'calendar%' "
        "  AND datetime(occurred_at) >= datetime(?) AND datetime(occurred_at) < datetime(?)",
        (USER_ID, starts_at, ends_before),
    ).fetchall()

    events: list[FixedEvent] = []
    for row in rows:
        try:
            payload = json.loads(str(row["raw_json"] or "{}"))
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("declined"):
            continue  # docs/07: declined events are excluded from capacity
        if str(payload.get("status", "confirmed")).lower() not in BUSY_STATUSES:
            continue
        try:
            starts = _aware(str(payload["starts_at"]), tz)
            ends = _aware(str(payload["ends_at"]), tz)
        except (KeyError, ValueError):
            continue
        events.append(
            FixedEvent(
                starts_at=starts,
                ends_at=ends,
                title=str(row["title"] or payload.get("title") or "Busy"),
                travel=bool(payload.get("travel")),
            )
        )
    return sorted(events, key=lambda e: e.starts_at)


def engagement_events(
    conn: sqlite3.Connection, day: date, tz: str, *, min_confidence: float = 0.0
) -> list[FixedEvent]:
    """Confirmed plans on `day`, as fixed events.

    `min_confidence` is the same gate the brief applies, and it belongs here for a
    stronger reason than symmetry. CLAUDE.md rule 2 keeps a low-confidence extraction
    out of the brief because it is not yet a fact; letting one silently delete seventy
    minutes from the owner's real capacity is the same error with a heavier consequence,
    since the brief at least renders nothing while the planner would quietly plan less
    work and never say why. It defaults to 0.0 so a caller that has no settings gets
    every row, and `compute` — which does have settings — always passes the threshold.

    A plan the owner has agreed to occupies the day exactly as a calendar event does —
    dinner at seven is not time available for deep work — so it is subtracted from
    capacity through the same path rather than through a parallel one.

    Two exclusions, both load-bearing:

      * `proposed` plans are not fixed. Someone suggesting Thursday is not Thursday, and
        reserving the evening for an invitation the owner has not answered would let
        anyone who emails them delete an evening from their week. Proposals reach the
        owner through the brief, which asks for a reply instead of assuming one.
      * A plan with a date but no clock time ("lunch on Friday") is not *placed*. There
        is no honest hour to give it, and midnight — what an ISO date parses to — would
        either sit outside the window silently or block the start of the day for
        something nobody said was in the morning.

        It is still returned, as an `allday` event that spends no capacity. That rule was
        always about refusing to delete time on a guess; dropping the plan from the day
        entirely was a side effect nobody chose, and it cost the owner six days of a
        summer programme the schedule never mentioned. A banner states the fact without
        pretending to know the hour.
    """
    # Selected on the local date prefix, NOT with datetime() against day bounds the way
    # fixed_events() reads the calendar. The two columns are different things: the
    # calendar connector writes a true instant with the event's own offset, while
    # engagement.starts_at holds whatever the message said, as it was said — the resolver
    # deliberately never converts timezones, so the column carries bare dates
    # ('2026-07-17'), naive local datetimes ('2026-07-17T19:00:00') and offset-bearing
    # ones side by side. Handing that mixture to SQLite's datetime() normalises the
    # offset-bearing rows to UTC and marches a 19:00 Phoenix dinner into the next day,
    # which is the failure tasks/lessons.md records for 2026-08-01. The leading ten
    # characters are the local day the message named, under every one of the three
    # shapes, and comparing them converts nothing.
    rows = conn.execute(
        "SELECT what, starts_at, ends_at, location FROM engagement "
        "WHERE user_id = ? AND status = 'confirmed' AND starts_at IS NOT NULL "
        "  AND confidence >= ? AND substr(starts_at, 1, 10) = ?",
        (USER_ID, min_confidence, day.isoformat()),
    ).fetchall()
    # A multi-day plan is only selected on the day it starts by the query above, so the
    # days in the middle of it are found separately — otherwise a six-day programme
    # appears on the Sunday and vanishes for the rest of the week.
    rows = list(rows) + list(
        conn.execute(
            "SELECT what, starts_at, ends_at, location FROM engagement "
            "WHERE user_id = ? AND status = 'confirmed' AND confidence >= ? "
            "  AND starts_at IS NOT NULL AND ends_at IS NOT NULL "
            "  AND substr(starts_at, 1, 10) < ? AND substr(ends_at, 1, 10) >= ? "
            "  AND length(starts_at) = 10",
            (USER_ID, min_confidence, day.isoformat(), day.isoformat()),
        ).fetchall()
    )

    events: list[FixedEvent] = []
    for row in rows:
        raw_start = str(row["starts_at"])
        if "T" not in raw_start and " " not in raw_start:
            events.append(_allday(row, day, tz))
            continue
        try:
            begins = _aware(raw_start, tz)
        except ValueError:
            continue
        raw_end = row["ends_at"]
        try:
            ends = _aware(str(raw_end), tz) if raw_end else begins + DEFAULT_ENGAGEMENT
        except ValueError:
            ends = begins + DEFAULT_ENGAGEMENT
        if ends <= begins:
            ends = begins + DEFAULT_ENGAGEMENT
        title = str(row["what"])
        if row["location"]:
            title = f"{title} — {row['location']}"
        events.append(FixedEvent(starts_at=begins, ends_at=ends, title=title))
    return sorted(events, key=lambda e: e.starts_at)


def _allday(row: sqlite3.Row, day: date, tz: str) -> FixedEvent:
    """One confirmed plan that named a day and no hour, as a banner over `day`.

    Spans local midnight to local midnight so it sorts to the top of the day and cannot
    be mistaken for an hour. It never reaches capacity arithmetic — `compute` drops
    every `allday` before subtracting anything — which is what lets the span be a full
    1440 minutes without deleting the day it describes.

    A run of days is numbered ("day 2 of 6") because the position is the useful part: on
    Wednesday of a six-day programme, "McKenna Summer Program" alone says nothing that
    Monday did not.
    """
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz)
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=zone)

    title = str(row["what"])
    if row["location"]:
        title = f"{title} — {row['location']}"
    start_day = date.fromisoformat(str(row["starts_at"])[:10])
    end_raw = row["ends_at"]
    end_day = date.fromisoformat(str(end_raw)[:10]) if end_raw else start_day
    span = (end_day - start_day).days + 1
    if span > 1:
        title = f"{title} (day {(day - start_day).days + 1} of {span})"

    return FixedEvent(
        starts_at=midnight,
        ends_at=midnight + timedelta(days=1),
        title=title,
        kind="allday",
    )


def routine_events(settings: Settings, day: date, tz: str) -> list[FixedEvent]:
    """The configured daily anchors — breakfast, lunch, gym — as fixed events on `day`.

    Life happens every day, so these carry no weekday gate: a Saturday breakfast is
    still breakfast. They come from config rather than the ledger because they are
    the owner's own template for a day, not something a source said.
    """
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz)
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=zone)
    return [
        FixedEvent(
            starts_at=midnight + timedelta(minutes=r.start_minute),
            ends_at=midnight + timedelta(minutes=r.start_minute + r.minutes),
            title=r.name.capitalize(),
            kind="routine",
        )
        for r in parse_routines(settings.routines)
    ]


def day_events(conn: sqlite3.Connection, settings: Settings, day: date) -> list[FixedEvent]:
    """The whole day's fixed picture: calendar + confirmed plans + routines, once each.

    This is the one builder for "what is already true about this day". `compute` clips
    it to the working window before doing capacity arithmetic; the planner persists it
    unclipped so a 7:15pm dinner is on the plan; the schedule page draws the same list
    live for days no planner has visited. Three consumers, one list — they cannot
    disagree about what the day holds.
    """
    tz = timezones.active_tz(settings, day)
    return sorted(
        _distinct(
            fixed_events(conn, day, tz)
            + engagement_events(conn, day, tz, min_confidence=settings.confidence_threshold)
            + routine_events(settings, day, tz)
        ),
        key=lambda e: e.starts_at,
    )


def _distinct(events: list[FixedEvent]) -> list[FixedEvent]:
    """One meeting counts once, however many sources described it.

    Not hypothetical: the owner's ledger holds 200 hand-imported `calendar:asu` rows and,
    once Calendar.app is connected, the same classes arrive again as `calendar:apple`.
    The two store the same instant differently — `2026-08-20T10:30:00-07:00` against
    `2026-08-20T17:30:00.000Z` — so nothing textual catches it, and the day's capacity
    collapsed from ten hours to ten minutes with every class subtracted twice.

    Identity is (title, start instant, end instant). Comparing instants is what makes the
    two spellings collapse, and it is only possible here because `_aware` has already
    resolved both. A genuinely distinct event does not share all three: two meetings at
    the same minute with the same title are one meeting.

    The travel flag is OR-ed rather than taken from the winner, so a source that knew a
    block was a commute is not silently overruled by one that did not.
    """
    kept: dict[tuple[str, datetime, datetime], FixedEvent] = {}
    for event in events:
        identity = (event.title.casefold(), event.starts_at, event.ends_at)
        seen = kept.get(identity)
        if seen is None or event.travel and not seen.travel:
            kept[identity] = event
    return list(kept.values())


def _aware(value: str, tz: str) -> datetime:
    """The instant this event starts, expressed in the owner's zone for that day.

    Converting rather than merely making it aware is what keeps the plan readable. The
    comparisons were always correct — two aware datetimes compare by instant whatever
    their offsets — but everything downstream *renders* with strftime, and a source that
    stores UTC then printed a 10:30 Phoenix lecture as "17:30". The owner's own calendar
    import stores `-07:00` and printed correctly, so the two sat side by side in one plan
    disagreeing by seven hours, and blocks appeared to be scheduled at 01:00.

    A naive value is read as already being in `tz`: it is a wall clock somebody wrote
    down, and there is nothing else it could mean.
    """
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def buffer_for(event: FixedEvent, settings: Settings) -> int:
    """docs/04 §1.2: 10 min after any meeting >= 30 min, 5 min otherwise.

    Routines get none: the buffer models the context-switch tax after a meeting, and
    lunch is not a meeting — charging ten minutes after it would quietly shrink every
    afternoon."""
    if event.kind == "routine":
        return 0
    if event.minutes >= settings.buffer_long_threshold_minutes:
        return settings.buffer_long_minutes
    return settings.buffer_short_minutes


def compute(
    conn: sqlite3.Connection,
    settings: Settings,
    day: date,
    *,
    events: list[FixedEvent] | None = None,
) -> Capacity:
    """P1. Compute capacity before selecting any work. Never select past it."""
    tz = timezones.active_tz(settings, day)
    window_start, window_end = timezones.window_on(settings, day)
    window_minutes = int((window_end - window_start).total_seconds() // 60)

    if not timezones.is_working_day(settings, day):
        # Not a working day at all. Zero capacity, and no slots, so nothing can be
        # scheduled into it by accident.
        return Capacity(
            day=day,
            tz=tz,
            window_minutes=0,
            fixed_minutes=0,
            buffer_minutes=0,
            travel_minutes=0,
            reserve_minutes=0,
            capacity_minutes=0,
            slots=[],
            _min_capacity=settings.min_capacity_minutes,
        )

    # An explicit `events` list is a caller supplying the whole fixed picture (the tests
    # do this, and so does any what-if); it is not extended from the ledger, or a caller
    # asking "what would the day look like with these three meetings" would silently get
    # a fourth.
    fixed = list(events) if events is not None else day_events(conn, settings, day)
    # The window filter belongs to this function and not to `day_events`: capacity is
    # "how much work fits between nine and six", so an evening dinner is correctly not
    # subtracted from it. The Schedule page asks a different question and calls the
    # reader without this line.
    # An all-day banner is a fact about the day, not an hour of it. It spans midnight to
    # midnight, so leaving it in would swallow the entire window and every day of a
    # six-day programme would report zero capacity. Dropped here rather than at the
    # reader, so nothing downstream of `compute` has to remember.
    fixed = [e for e in fixed if not e.allday]
    fixed = [e for e in fixed if e.ends_at > window_start and e.starts_at < window_end]
    fixed.sort(key=lambda e: e.starts_at)

    fixed_minutes = 0
    travel_minutes = 0
    buffer_minutes = 0
    #: Each event occupies its own span plus the buffer that follows it, and the two are
    #: subtracted together so a back-to-back pair cannot claim the same minute twice.
    occupied: list[tuple[datetime, datetime]] = []
    for event in fixed:
        start = max(event.starts_at, window_start)
        end = min(event.ends_at, window_end)
        minutes = max(0, int((end - start).total_seconds() // 60))
        if event.travel:
            travel_minutes += minutes
        else:
            fixed_minutes += minutes
        pad = buffer_for(event, settings)
        buffer_minutes += pad
        occupied.append((start, min(end + timedelta(minutes=pad), window_end)))

    slots = _free_slots(window_start, window_end, occupied, settings.min_block_minutes)
    reserve = max(1, settings.daily_reserve_minutes)  # "never zero"
    free_minutes = sum(slot.minutes for slot in slots)

    from backglass.goals import reviews as reviews_mod

    # Capped: a monster backlog is a planning decision the owner should see (the
    # brief reports the uncapped estimate), not a silent zeroing of the whole day.
    review_minutes = min(reviews_mod.review_minutes(conn, day)[0], REVIEW_CAP_MINUTES)

    capacity_minutes = max(
        0,
        min(free_minutes, window_minutes - fixed_minutes - travel_minutes - buffer_minutes)
        - reserve
        - review_minutes,
    )

    return Capacity(
        day=day,
        tz=tz,
        window_minutes=window_minutes,
        fixed_minutes=fixed_minutes,
        buffer_minutes=buffer_minutes,
        travel_minutes=travel_minutes,
        reserve_minutes=reserve,
        capacity_minutes=capacity_minutes,
        review_minutes=review_minutes,
        slots=slots,
        fixed=fixed,
        _min_capacity=settings.min_capacity_minutes,
    )


def _free_slots(
    window_start: datetime,
    window_end: datetime,
    occupied: list[tuple[datetime, datetime]],
    min_minutes: int,
) -> list[Slot]:
    """The gaps between fixed events, merged, and discarded when too small to use.

    P5: "Never schedule across a fixed event. Never propose overlapping blocks." Producing
    slots rather than a single minute count is what makes that structural — the planner
    can only ever place work inside one of these.
    """
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(occupied):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    slots: list[Slot] = []
    cursor = window_start
    for start, end in merged:
        if start > cursor:
            slots.append(Slot(cursor, min(start, window_end)))
        cursor = max(cursor, end)
    if cursor < window_end:
        slots.append(Slot(cursor, window_end))
    return [slot for slot in slots if slot.minutes >= min_minutes]
