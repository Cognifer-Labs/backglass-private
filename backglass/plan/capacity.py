"""The capacity model. docs/04 §1.2.

    "The single most common failure of automated day planning is proposing eight hours of
    work into a day that has five hours of meetings. So capacity is computed first, and it
    is a hard constraint rather than a display value."

    working_window   = configured per weekday (default 09:00–18:00 local)
    fixed            = calendar events marked busy, minus declined
    buffer           = 10 min after any meeting >= 30 min, 5 min otherwise
    commute/travel   = any calendar event tagged travel, plus its buffer
    capacity_minutes = working_window − fixed − buffer − travel − reserve
    reserve          = configured slack, default 45 min/day, never zero

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

from backglass.config import Settings
from backglass.ledger import USER_ID
from backglass.plan import timezones

#: docs/07 §Calendar: "Declined events are excluded from capacity. Tentative events count
#: as busy." Tentative counting as busy is the conservative direction — under-promising
#: capacity costs one unscheduled hour, over-promising costs a missed commitment.
BUSY_STATUSES = {"confirmed", "tentative", "busy"}


@dataclass(frozen=True)
class FixedEvent:
    starts_at: datetime
    ends_at: datetime
    title: str = ""
    travel: bool = False

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
    slots: list[Slot] = field(default_factory=list)
    fixed: list[FixedEvent] = field(default_factory=list)

    @property
    def plannable(self) -> bool:
        """P3. "If capacity is under 60 minutes, do not propose a plan."."""
        return self.capacity_minutes >= self._min_capacity

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
    rows = conn.execute(
        "SELECT raw_json, title FROM source_item "
        "WHERE user_id = ? AND source LIKE 'calendar%' AND date(occurred_at) = date(?)",
        (USER_ID, day.isoformat()),
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


def _aware(value: str, tz: str) -> datetime:
    from zoneinfo import ZoneInfo

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz))
    return parsed


def buffer_for(event: FixedEvent, settings: Settings) -> int:
    """docs/04 §1.2: 10 min after any meeting >= 30 min, 5 min otherwise."""
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

    fixed = events if events is not None else fixed_events(conn, day, tz)
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
    capacity_minutes = max(
        0,
        min(free_minutes, window_minutes - fixed_minutes - travel_minutes - buffer_minutes)
        - reserve,
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
