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

Routines — the configured anchors, breakfast through the evening — are part of that
fixed picture, and docs/04 §1.9 governs where they land: the configured hour is
preferred rather than decreed (P17), a routine that cannot be placed clear of everything
keeps its hour and says what it overlaps (P18), and a pinned one never moves (P19).

Fixed events come from `source_item` rows written by the calendar connector, which is
Phase 5. Until it exists this returns an empty list and capacity is the whole window minus
the reserve — which is correct, not a stub: a day with no known meetings genuinely has no
known meetings.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field, replace
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

#: How far a flexible routine may drift from its preferred hour to find a free gap.
#: Unbounded, a fully-booked afternoon puts lunch at four o'clock, and a meal moved that
#: far is not the meal the owner asked for — it is the planner quietly rewriting the day
#: and calling it lunch. Two hours is the width of "around then"; past it the routine
#: keeps its hour and says what it collided with, which is a fact the owner can act on.
ROUTINE_MAX_SHIFT_MINUTES = 120

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
    #: Where it happens, when the source said. Only the calendar reader sets it, and it
    #: exists for one purpose: two classes in two buildings need a walk between them, and
    #: nothing else in the day can tell you whether the owner has to move.
    location: str = ""
    #: Set only on a routine that could not be placed clear of everything else: the
    #: sentence naming what it overlaps and why it did not move. It rides on the event
    #: rather than being returned alongside it because all three consumers of
    #: `day_events` want it — the plan notes it, the schedule can show it — and a fact
    #: returned on the side is a fact two of them will forget to ask for.
    conflict: str = ""
    #: The `source_item` this event was read out of, when there is one. Only the
    #: calendar reader sets it: a routine is configuration and a confirmed plan is an
    #: extraction, and neither is a row the owner can point at and call wrong.
    #:
    #: It exists so the Schedule page can offer the door that was missing on
    #: 2026-08-27, when the CHM 113 lab moved to the morning and the day page went on
    #: drawing the evening one. The ledger knew — the fix was a script, because nothing
    #: the owner could click knew which row to retract.
    source_item_id: int | None = None

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
    #: The window existed today and has already closed — set only when `not_before`
    #: clamped it past the end. A third fact, and stored rather than derived because it
    #: is the one thing a zero window cannot tell you about itself.
    window_closed: bool = False

    @property
    def largest_slot(self) -> int:
        """The longest contiguous free run, which is the biggest block that can be placed.

        `capacity_minutes` is a **sum** and placement needs **contiguity**; before this
        existed nothing reconciled them. Measured on the owner's real 2026-08-26: 215
        minutes of capacity in six fragments whose largest was 60, so `select` spent its
        budget on a 90-minute sitting that `place` could not put anywhere, and 175 of 183
        candidates overflowed on a day with three and a half free hours.

        Zero when the day has no free slot at all, which is the honest answer and the one
        `select` needs — a day with no hole can hold no block.
        """
        return max((slot.minutes for slot in self.slots), default=0)

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
        sends them looking for meetings that do not exist.

        This was derived from `window_minutes == 0` alone, on the stated grounds that the
        non-working branch was the only one that could return a zero window. `not_before`
        made that false — a run at 20:47 against a window that closed at 18:00 zeroes it
        too — and the first thing the clamp did on the live ledger was report a Saturday
        the owner works as "not a working day". `window_closed` is the third fact, and it
        is excluded here rather than folded in, because "the day is over" and "there is no
        such day" send the owner to different places.
        """
        return self.window_minutes == 0 and not self.window_closed

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
    return _calendar_span(conn, starts_at, ends_before, tz)


def fixed_events_between(
    conn: sqlite3.Connection, first: date, last_exclusive: date, tz: str
) -> list[FixedEvent]:
    """The same read over a span of days, in one query rather than one per day.

    `datetime(si.occurred_at)` is a function on the column, so no index can serve it and
    every call is a scan of all 11k source items. That is 14ms, which is invisible for a
    day and 0.5s for the Homework month grid's 42 of them. The scan is unavoidable — the
    stored offsets are what make it necessary, and utc_bounds carries that reasoning —
    but doing it forty-two times is not.

    Returns the span unbucketed: which day an event belongs to is the caller's question,
    because a caller that spans a timezone change has two answers for the same instant.
    """
    starts_at, ends_before = timezones.utc_bounds(first, last_exclusive, tz)
    return _calendar_span(conn, starts_at, ends_before, tz)


def _calendar_span(
    conn: sqlite3.Connection, starts_at: str, ends_before: str, tz: str
) -> list[FixedEvent]:
    # `NOT EXISTS` rather than a status column, because `source_item` is immutable and
    # the row stays true forever: the class really did meet on Wednesdays until the 10th.
    # What changed is that Calendar.app no longer has it, which migration 0030 records
    # beside the item. Without this join the planner keeps subtracting dropped classes —
    # five of them, for ten days, which is what sent the owner looking.
    rows = conn.execute(
        "SELECT si.id, si.raw_json, si.title FROM source_item si "
        "WHERE si.user_id = ? AND si.source LIKE 'calendar%' "
        "  AND datetime(si.occurred_at) >= datetime(?) "
        "  AND datetime(si.occurred_at) < datetime(?) "
        "  AND NOT EXISTS (SELECT 1 FROM source_item_retraction r "
        "                  WHERE r.source_item_id = si.id)",
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
                location=str(payload.get("location") or ""),
                source_item_id=int(row["id"]),
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
        "SELECT what, starts_at, ends_at, location, confidence FROM engagement "
        "WHERE user_id = ? AND status = 'confirmed' AND starts_at IS NOT NULL "
        "  AND confidence >= ? AND substr(starts_at, 1, 10) = ?",
        (USER_ID, min_confidence, day.isoformat()),
    ).fetchall()
    # A multi-day plan is only selected on the day it starts by the query above, so the
    # days in the middle of it are found separately — otherwise a six-day programme
    # appears on the Sunday and vanishes for the rest of the week. Deliberately not
    # restricted to date-only starts: a run of days written with a clock on its first
    # ("2026-08-10T09:00" through "2026-08-14") is still a run of days, and excluding it
    # left the plan visible on day one as a 5220-minute block that ate the whole window
    # and absent from the four days after.
    rows = list(rows) + list(
        conn.execute(
            "SELECT what, starts_at, ends_at, location, confidence FROM engagement "
            "WHERE user_id = ? AND status = 'confirmed' AND confidence >= ? "
            "  AND starts_at IS NOT NULL AND ends_at IS NOT NULL "
            "  AND substr(starts_at, 1, 10) < ? AND substr(ends_at, 1, 10) >= ?",
            (USER_ID, min_confidence, day.isoformat(), day.isoformat()),
        ).fetchall()
    )

    candidates: list[Candidate] = []
    for row in rows:
        raw_start = str(row["starts_at"])
        span = _span(row)
        if "T" not in raw_start and " " not in raw_start or span[1] > span[0]:
            # A day and no hour, or a run of days however it was written. A plan that
            # spans more than one day cannot be an hour on any of them: read literally,
            # "2026-08-10T09:00" to "2026-08-14" is a 5220-minute event that swallows
            # the whole of the 10th's window and appears on none of the four days after
            # it. A banner is the only honest reading.
            candidates.append(Candidate(_allday(row, day, tz), row, span))
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
        candidates.append(
            Candidate(FixedEvent(starts_at=begins, ends_at=ends, title=title), row, span)
        )
    return sorted(_one_plan_per_plan(candidates), key=lambda e: e.starts_at)


def _span(row: sqlite3.Row) -> tuple[date, date]:
    """The run of local days a plan covers, as (first, last). One day means first == last."""
    start = date.fromisoformat(str(row["starts_at"])[:10])
    raw_end = row["ends_at"]
    try:
        end = date.fromisoformat(str(raw_end)[:10]) if raw_end else start
    except ValueError:
        end = start
    return (start, max(start, end))


@dataclass(frozen=True)
class Candidate:
    """One engagement row on its way to becoming a day's event.

    Carries the two things the collapse below needs and nothing downstream wants: the
    row it came from, and the run of days it covers. Keeping them here rather than on
    `FixedEvent` is what stops a rendering concern from leaking into the type every
    consumer of the module reads.
    """

    event: FixedEvent
    row: sqlite3.Row
    span: tuple[date, date]

    @property
    def confidence(self) -> float:
        return float(self.row["confidence"] or 0.0)

    @property
    def tokens(self) -> set[str]:
        """Words from what the plan IS — never from where it is.

        `engagement_events` appends " — {location}" to the title before this runs, and
        a venue is not evidence of identity: two different plans at "Armstrong Hall
        Rotunda, 1100 S McAllister Ave, Tempe" share five long words and are two
        different plans. Reading `what` alone is what keeps a room from merging the
        meetings held in it.
        """
        return _tokens(str(self.row["what"]))


#: Words too short to identify anything. Five characters is not a rule about English, it
#: is where "with", "and", "at" and the rest stop being evidence — "lunch with Sarah" and
#: "lunch with Tom" share two four-letter tokens and are two different lunches.
_MIN_TOKEN = 5

#: How many distinctive words two titles must share before they are believed to be one
#: plan. One is not enough: "dinner" alone would fold a family dinner into a work dinner.
_SHARED_TOKENS = 2


def _tokens(title: str) -> set[str]:
    return {
        word
        for word in "".join(c.lower() if c.isalnum() else " " for c in title).split()
        if len(word) >= _MIN_TOKEN
    }


def _one_plan_per_plan(candidates: list[Candidate]) -> list[FixedEvent]:
    """Collapse several extractions of the SAME plan into the one the model believed most.

    The owner's day held "McKenna Program Welcome Dinner" 18:00–20:00, "McKenna Summer
    Program kickoff dinner" 18:30–19:30 and "college dinner appointment" 18:30–19:30 —
    three rows, three renderings, one meal. `_distinct` cannot help: it collapses exact
    (title, start, end) triples, which is right for two calendars describing one meeting
    and useless against three readings of one invitation.

    Two conditions, and both are needed. The intervals must overlap, and the titles must
    share at least two words of five or more characters. Overlap alone would merge a real
    double-booking, which is the one thing the owner most needs to see; shared words alone
    would merge next Tuesday's standup into this one.

    Deliberately conservative in the direction that costs less. A duplicate that survives
    is visible and can be dismissed; a genuine conflict that gets merged is a meeting
    silently deleted, and the owner never learns it was there. So "college dinner
    appointment" above stays separate — it shares only "dinner" with the other two — and
    that is the correct failure.

    An all-day banner is never merged with a timed plan, whatever the words say. It
    spans midnight to midnight, so it overlaps everything on the day by construction —
    without this guard a "BioBridge Early Start Program" banner would swallow a
    "BioBridge Early Start orientation" at 9am and delete a real hour from the schedule.
    Same kind only: a banner can absorb a banner, an hour can absorb an hour.

    And between two banners, overlap carries no information for the same reason, so they
    are compared on the run of days they cover instead. "McKenna Summer Program"
    (9th–14th) and "McKenna Research Program" (10th–12th) share two long words and every
    day of the shorter one; matched on overlap they would delete each other on alternate
    days and leave a six-day banner with a hole in the middle. Identical spans are one
    programme described twice; different spans are two programmes.

    Nothing is written. The ledger keeps every row, so a merge here changes what the day
    renders, never what the owner can go and look at.
    """
    kept: list[Candidate] = []
    for candidate in sorted(candidates, key=lambda c: -c.confidence):
        event, tokens = candidate.event, candidate.tokens
        duplicate = False
        for winner in kept:
            if event.allday != winner.event.allday:
                continue
            same_occasion = (
                candidate.span == winner.span
                if event.allday
                else event.starts_at < winner.event.ends_at
                and winner.event.starts_at < event.ends_at
            )
            if same_occasion and len(tokens & winner.tokens) >= _SHARED_TOKENS:
                duplicate = True
                break
        if not duplicate:
            kept.append(candidate)
    return [c.event for c in kept]


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


def routine_events(
    settings: Settings,
    day: date,
    tz: str,
    around: list[FixedEvent] | None = None,
) -> list[FixedEvent]:
    """The configured anchors — breakfast, lunch, gym — placed on `day` around `around`.

    Most of life happens every day, so an unscoped routine carries no weekday gate: a
    Saturday breakfast is still breakfast. A routine that names days is filtered to
    them, which is what lets a weekly obligation be one — Banner volunteering on
    Wednesdays 4–8pm had to be either an everyday routine, deleting four hours from six
    days that do not have it, or nothing at all, leaving the planner free to book over
    the one day that does.

    They come from config rather than the ledger because they are the owner's own
    template for a day, not something a source said. And the hour in that template is a
    preference: `around` is everything already true about the day — classes, meetings,
    confirmed engagements — and a routine whose preferred span lands inside one of them
    moves to the nearest free gap that fits it. The owner ruled on 2026-08-21 that meals
    and the gym are planned *around* the fixed day rather than stamped on top of it;
    until then the plan for the 24th ate lunch inside a chemistry class.

    P17 and P18, docs/04 §1.9.

    Pinned routines (`banner@16:00+240!@wed`) are placed first and never move (P19) — they are
    obligations at a stated hour — and every placed routine joins the obstacle set, so
    breakfast cannot be shifted onto lunch.
    """
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz)
    midnight = datetime.combine(day, datetime.min.time(), tzinfo=zone)
    day_end = midnight + timedelta(days=1)
    routines = [r for r in parse_routines(settings.routines) if r.falls_on(day)]

    # An all-day banner covers every minute of the day; treating it as an obstacle would
    # move every routine off the day entirely, or (bounded) fail to place any of them.
    obstacles: list[tuple[datetime, datetime, str]] = [
        (e.starts_at, e.ends_at, e.title) for e in (around or []) if not e.allday
    ]

    placed: list[FixedEvent] = []
    # Pinned first, so a flexible routine yields to a decreed one rather than the order
    # of the config line deciding which of the two moves.
    for routine in sorted(routines, key=lambda r: (not r.pinned, r.start_minute)):
        preferred = midnight + timedelta(minutes=routine.start_minute)
        span = timedelta(minutes=routine.minutes)
        title = routine.name.capitalize()
        conflict = ""
        start = preferred

        if not routine.pinned:
            found = _nearest_gap(preferred, span, obstacles, midnight, day_end)
            if found is not None:
                start = found
            else:
                clashing = _clashing_titles(preferred, preferred + span, obstacles)
                if clashing:
                    # Never silently dropped and never silently moved out of the day: the
                    # routine keeps its hour and states what it is inside. P2's rule, one
                    # register down — a meal the planner could not place is a fact about
                    # a day too full to eat in, which is worth saying out loud.
                    conflict = (
                        f"{title} overlaps {clashing} — no free {routine.minutes}m gap "
                        f"within {ROUTINE_MAX_SHIFT_MINUTES}m of "
                        f"{preferred.strftime('%H:%M')}."
                    )

        placed.append(
            FixedEvent(
                starts_at=start,
                ends_at=start + span,
                title=title,
                kind="routine",
                conflict=conflict,
            )
        )
        obstacles.append((start, start + span, title))

    return sorted(placed, key=lambda e: e.starts_at)


def _overlaps(
    start: datetime, end: datetime, obstacles: list[tuple[datetime, datetime, str]]
) -> bool:
    return any(o_start < end and start < o_end for o_start, o_end, _ in obstacles)


def _clashing_titles(
    start: datetime, end: datetime, obstacles: list[tuple[datetime, datetime, str]]
) -> str:
    names = [t for o_start, o_end, t in obstacles if o_start < end and start < o_end and t]
    return ", ".join(dict.fromkeys(names))


def _nearest_gap(
    preferred: datetime,
    span: timedelta,
    obstacles: list[tuple[datetime, datetime, str]],
    day_start: datetime,
    day_end: datetime,
) -> datetime | None:
    """The free start closest to `preferred`, earlier winning ties, or None.

    Only three kinds of start can ever be the closest free one: the preferred minute
    itself, the minute an obstacle ends, and the minute `span` before an obstacle
    begins. Anything else is either occupied or strictly further from `preferred` than
    one of those, so the search is over a handful of candidates rather than a scan of
    the day — which matters less for speed than for determinism. The same day must
    place lunch on the same minute every time it is planned, or 0028's
    `inputs_fingerprint` reports drift that is only arithmetic, and rule 3's "two runs,
    zero writes" stops holding.
    """
    candidates = {preferred}
    for o_start, o_end, _ in obstacles:
        candidates.add(o_end)
        candidates.add(o_start - span)

    best: tuple[int, datetime] | None = None
    for start in sorted(candidates):
        if start < day_start or start + span > day_end:
            continue
        drift = abs(int((start - preferred).total_seconds() // 60))
        if drift > ROUTINE_MAX_SHIFT_MINUTES:
            continue
        if _overlaps(start, start + span, obstacles):
            continue
        # Sorted ascending and compared strictly, so of two placements equally far from
        # the preferred hour the earlier one wins: eating before the class beats eating
        # after it, and either beats a coin toss.
        key = (drift, start)
        if best is None or key < best:
            best = key
    return best[1] if best is not None else None


def calendar_events(
    conn: sqlite3.Connection, settings: Settings, day: date
) -> list[FixedEvent]:
    """The day's calendar rows and confirmed plans, once each.

    The skeleton `day_events` builds on, published because a second caller wants exactly
    this and nothing else: the Homework month grid draws what the owner has to remember,
    and a routine is configuration and a walk is the cost of an event — neither is a
    thing to remember. Extracting it also means that grid cannot drift from the planner
    about what a Tuesday holds, which is the whole reason `day_events` was made the one
    builder in the first place.
    """
    tz = timezones.active_tz(settings, day)
    return sorted(
        _distinct(
            fixed_events(conn, day, tz)
            + engagement_events(conn, day, tz, min_confidence=settings.confidence_threshold)
        ),
        key=lambda e: e.starts_at,
    )


def calendar_events_between(
    conn: sqlite3.Connection, settings: Settings, first: date, last: date
) -> dict[date, list[FixedEvent]]:
    """`calendar_events` for every day from `first` to `last` inclusive, in one pass.

    Grouped by zone before it is grouped by day, because the owner moves between UTC-7
    and UTC+5:30 and a month can straddle the move. Each zone gets one scan and each
    event is kept only by the days that zone actually governs, so an instant near the
    boundary is filed under the day the owner would call it and not under both.
    """
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    by_zone: dict[str, list[date]] = {}
    for day in days:
        by_zone.setdefault(timezones.active_tz(settings, day), []).append(day)

    out: dict[date, list[FixedEvent]] = {day: [] for day in days}
    for tz, zone_days in by_zone.items():
        governed = set(zone_days)
        span = fixed_events_between(
            conn, zone_days[0], zone_days[-1] + timedelta(days=1), tz
        )
        for event in span:
            day = event.starts_at.date()
            if day in governed:
                out[day].append(event)
        for day in zone_days:
            # Confirmed plans stay per-day: they are a handful of rows read through an
            # index, and the scan this function exists to collapse is not theirs.
            out[day] = sorted(
                _distinct(
                    out[day]
                    + engagement_events(
                        conn, day, tz, min_confidence=settings.confidence_threshold
                    )
                ),
                key=lambda e: e.starts_at,
            )
    return out


def day_events(conn: sqlite3.Connection, settings: Settings, day: date) -> list[FixedEvent]:
    """The whole day's fixed picture: calendar + confirmed plans + routines, once each.

    This is the one builder for "what is already true about this day". `compute` clips
    it to the working window before doing capacity arithmetic; the planner persists it
    unclipped so a 7:15pm dinner is on the plan; the schedule page draws the same list
    live for days no planner has visited. Three consumers, one list — they cannot
    disagree about what the day holds.
    """
    tz = timezones.active_tz(settings, day)
    # Calendar and engagements first, deduped: they are the skeleton the day is built
    # on, and they are what routines are placed *around*. Deduping before placement is
    # what stops the same class, arriving twice from two calendars, being treated as two
    # obstacles a meal has to dodge separately.
    booked = calendar_events(conn, settings, day)
    # Walks are computed from `booked` and added before the routines are placed, so a
    # meal looking for a free gap treats the walk as occupied — the alternative puts
    # lunch inside the fifteen minutes the owner is crossing campus.
    booked = booked + walks_between(booked, settings)
    return sorted(
        _distinct(booked + routine_events(settings, day, tz, around=booked)),
        key=lambda e: e.starts_at,
    )


@dataclass(frozen=True)
class Yield:
    """One routine that gave up time, and how much. P18's rule, applied to relief.

    Carried so the plan can say it. "Relax gave up 60m" is the sentence that makes the
    owner's evening disappearing a decision they can see and reverse, rather than a
    thing they notice at nine o'clock.
    """

    title: str
    minutes: int
    removed: bool


def relieved(
    events: list[FixedEvent], settings: Settings, *, compress: bool = False
) -> tuple[list[FixedEvent], list[Yield]]:
    """The day's fixed picture with the sacrificial routines yielded.

    Owner's ruling, 2026-08-27: "if things dont fit remove relax time and mcat study time
    or reduce time for other things." Only ever applied on a second pass, and only for
    work that is genuinely deadline-pressed — see `planner.propose`. The backlog is 255
    hours and will never fit; if "does not fit" meant the backlog, Relax would be deleted
    every day for the rest of the semester, which is not what anybody asked for.

    Two registers, because the owner named two. What is on `relief_sacrificial` is
    removed outright; what is on `relief_compressible` is shortened to a floor and never
    deleted. Everything else — the meals, the shower — is untouched and must stay that
    way: it was not named, and a planner that skips dinner to fit a practice quiz is one
    that gets turned off.

    `compress` is the second register and it is off by default, because the owner's word
    was "or": *remove relax and mcat study time, **or** reduce time for other things*.
    That is an escalation, not a list — so the first relief pass gives up the evening and
    stops, and only a day that still cannot fit its deadline-pressed work goes on to
    shorten the gym. Without the distinction the first run of this cut an hour of
    exercise on a day that had already found the room without it.

    Only routines are eligible. A class cannot yield, a confirmed plan with another
    person in it cannot yield, and an all-day banner is not an hour to take.
    """
    sacrificial = _names(settings.relief_sacrificial)
    compressible = _names(settings.relief_compressible)
    floor = min(1.0, max(0.0, settings.relief_compress_floor))

    kept: list[FixedEvent] = []
    yields: list[Yield] = []
    for event in events:
        if event.kind not in ("routine", "study"):
            kept.append(event)
            continue
        name = event.title.strip().lower()
        if _matches(name, sacrificial):
            yields.append(Yield(title=event.title, minutes=event.minutes, removed=True))
            continue
        if compress and _matches(name, compressible):
            shortened = int(event.minutes * floor)
            if shortened >= event.minutes:
                kept.append(event)
                continue
            yields.append(
                Yield(title=event.title, minutes=event.minutes - shortened, removed=False)
            )
            kept.append(
                replace(event, ends_at=event.starts_at + timedelta(minutes=shortened))
            )
            continue
        kept.append(event)
    return kept, yields


def _names(configured: list[str]) -> set[str]:
    return {name.strip().lower() for name in configured if name.strip()}


def _matches(name: str, names: set[str]) -> bool:
    """A routine's title against a configured name.

    Substring rather than equality, because the two vocabularies are written by different
    hands: the routine is spelled `relax` in `ROUTINES` and renders as "Relax", while the
    standing block P20 places is titled "Study" and a goal target is "MCAT practice
    sections". Matching on containment lets one setting cover all three spellings of the
    same intention.
    """
    return any(candidate in name for candidate in names)


def relief_window_end(settings: Settings, day: date) -> datetime | None:
    """How late the day may run under relief, or None when relief is switched off.

    Never earlier than the normal window's end — relief only ever extends a day, and a
    misconfigured value that would shorten one is ignored rather than obeyed.
    """
    raw = settings.relief_window_end.strip()
    if not raw:
        return None
    _, normal_end = timezones.window_on(settings, day)
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
        extended = normal_end.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    return max(normal_end, extended)


def walks_between(events: list[FixedEvent], settings: Settings) -> list[FixedEvent]:
    """The walk from one room to the next, as travel the day has to pay for.

    Owner's ask, 2026-08-27: "also account for travel and walk to classes." `capacity`
    has subtracted travel since it was written, and nothing ever produced any: the flag
    lives in a calendar event's `raw_json` and the ASU registrar import does not carry
    one. Measured that afternoon, both a Friday and a Monday reported `travel 0m` while
    the Monday ran CIS 236 in BA 396, BIO 181 in MUR 101, CHM 113 in LSA 191 and LSB 191
    in ARM L1-17 — four buildings, and every gap between them offered to the planner as
    time to sit down and work in.

    A walk is inserted before the later of two consecutive events **only when the day
    actually requires one**: both events name a room, the rooms differ, and there is a
    gap to put it in. All three matter. A routine has no room, so lunch never generates a
    walk; two sections of the same class in the same room do not either; and back-to-back
    classes with no gap get nothing, because a walk drawn over a class the owner is
    sitting in would be a plan asserting they are in two places.

    Sized to fit rather than to the setting: a 25-minute gap between two buildings buys a
    15-minute walk and leaves 10, and a 10-minute gap becomes 10 minutes of walking and
    no free time — which is the true reading of that gap, and the opposite of what the
    planner believed before this existed.

    Emitted as ordinary travel events, so nothing downstream needs to learn a new idea:
    `compute` already subtracts `travel` spans, the plan already renders them, and
    `buffer_for` already declines to charge a meeting buffer on top of one.
    """
    walk = max(0, settings.walk_minutes)
    if walk == 0:
        return []
    timed = sorted(
        (e for e in events if not e.allday and e.location.strip()),
        key=lambda e: e.starts_at,
    )
    out: list[FixedEvent] = []
    for earlier, later in zip(timed, timed[1:], strict=False):
        if _same_place(earlier.location, later.location):
            continue
        gap = int((later.starts_at - earlier.ends_at).total_seconds() // 60)
        if gap <= 0:
            # Overlapping, or genuinely back to back. There is no minute to put a walk
            # in, and inventing one would either overlap a class or move it. The honest
            # answer is that this day is already impossible in a way the calendar is not
            # telling anyone, and that is `_distinct`'s and the planner's business.
            continue
        minutes = min(walk, gap)
        out.append(
            FixedEvent(
                starts_at=later.starts_at - timedelta(minutes=minutes),
                ends_at=later.starts_at,
                title=f"Walk to {later.location}",
                travel=True,
                location=later.location,
            )
        )
    return out


def _same_place(a: str, b: str) -> bool:
    """Two room strings naming one room.

    Compared on their words rather than exactly, because the same building reaches the
    ledger spelled more than one way — "Tempe PSD 228" from the registrar import and
    "PSD 228" from Calendar.app are one room, and a walk between them would be fifteen
    minutes charged for standing still.
    """
    return _place_words(a) == _place_words(b)


def _place_words(value: str) -> frozenset[str]:
    #: "Tempe" is on every room the owner has, so it distinguishes nothing and dropping
    #: it is what lets the two spellings above compare equal.
    return frozenset(re.sub(r"[^a-z0-9]+", " ", value.lower()).split()) - {"tempe"}


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

    `source_item_id` survives that swap for the same reason. Only the calendar reader
    sets it, and the swap hands the slot to whichever copy knew about travel — so an
    engagement that knew a dinner was a commute would otherwise take the slot and drop
    the calendar row's identity on the way, leaving the Schedule page an event it can
    draw and cannot let the owner correct.
    """
    kept: dict[tuple[str, datetime, datetime], FixedEvent] = {}
    for event in events:
        identity = (event.title.casefold(), event.starts_at, event.ends_at)
        seen = kept.get(identity)
        if seen is None:
            kept[identity] = event
        elif event.travel and not seen.travel:
            kept[identity] = replace(
                event, source_item_id=event.source_item_id or seen.source_item_id
            )
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
    not_before: datetime | None = None,
    until: datetime | None = None,
) -> Capacity:
    """P1. Compute capacity before selecting any work. Never select past it.

    `until` extends the window's end, and only ever extends it — it is the relief pass's
    door (owner's ruling 2026-08-27) and the mirror image of `not_before`, which only
    ever clamps the start. A day that fits never passes it, so the ordinary plan is the
    ordinary window and nothing about it changed.

    `not_before` clamps the start of the window, which is how a plan built at four in the
    afternoon describes the afternoon rather than the morning. It is passed by the CLI
    and the catch-up net, both of which know the wall clock; it defaults to None so that
    every test and every what-if measures a whole, deterministic window.

    Hours that have already happened are not capacity. Without the clamp a run at 17:22
    reported 281 minutes of a 480-minute window and then packed breakfast at 07:30 —
    arithmetic that is internally consistent and describes a day that is over.
    """
    tz = timezones.active_tz(settings, day)
    window_start, window_end = timezones.window_on(settings, day)
    if until is not None:
        window_end = max(window_end, until)
    window_closed = False
    if not_before is not None and window_start < not_before:
        # Clamped, never extended: a run before the window opens still plans the whole
        # window, because the morning has not been spent yet.
        window_start = min(not_before, window_end)
        window_closed = window_start >= window_end
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

    #: Each event occupies its own span plus the buffer that follows it, and the two are
    #: subtracted together so a back-to-back pair cannot claim the same minute twice.
    occupied: list[tuple[datetime, datetime]] = []
    travel_spans: list[tuple[datetime, datetime]] = []
    event_spans: list[tuple[datetime, datetime]] = []
    for event in fixed:
        start = max(event.starts_at, window_start)
        end = min(event.ends_at, window_end)
        (travel_spans if event.travel else event_spans).append((start, end))
        pad = buffer_for(event, settings)
        occupied.append((start, min(end + timedelta(minutes=pad), window_end)))

    # Counted as spans, not as a running total. The old code summed each event's length,
    # which charges the day twice for any minute two events both cover — and the owner's
    # class schedule overlaps constantly. On 2026-08-26 it reported 545 minutes of fixed
    # time where the union is 415, leaving the planner to believe an eighty-minute day
    # was all that remained of one holding three and a half free hours. It failed the
    # quiet way: a short day looks like a busy day, not like a bug, and P3 would
    # eventually decline to plan a day that was perfectly plannable.
    #
    # `_free_slots` had it right all along — it merges before measuring — so the two
    # halves of the formula below disagreed only when events overlapped, and `min` took
    # the wrong one every time.
    travel_minutes = _span_minutes(travel_spans)
    # Travel first, so a minute covered by both is charged once and to the more specific
    # of the two. The parts are therefore disjoint and sum to `occupied_minutes`.
    fixed_minutes = _span_minutes(event_spans + travel_spans) - travel_minutes
    occupied_minutes = _span_minutes(occupied)
    # What the buffers added beyond the events themselves, which is what a reader of the
    # capacity line means by it. A buffer landing inside the next event costs nothing and
    # now says so, instead of being added and then silently double-counted.
    buffer_minutes = occupied_minutes - fixed_minutes - travel_minutes

    slots = _free_slots(window_start, window_end, occupied, settings.min_block_minutes)
    reserve = max(1, settings.daily_reserve_minutes)  # "never zero"
    free_minutes = sum(slot.minutes for slot in slots)

    from backglass.goals import reviews as reviews_mod

    # Capped: a monster backlog is a planning decision the owner should see (the
    # brief reports the uncapped estimate), not a silent zeroing of the whole day.
    review_minutes = min(reviews_mod.review_minutes(conn, day)[0], REVIEW_CAP_MINUTES)

    capacity_minutes = max(
        0,
        min(free_minutes, window_minutes - occupied_minutes)
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
        window_closed=window_closed,
        _min_capacity=settings.min_capacity_minutes,
    )


def _merge(spans: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Overlapping spans collapsed into disjoint ones, in order.

    An hour two things both cover is one hour. That is obvious stated this way and was
    not obvious in `compute`, which summed each event's length and so charged the day
    twice for it — see `_span_minutes`.
    """
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _span_minutes(spans: list[tuple[datetime, datetime]]) -> int:
    """How many minutes a set of spans actually covers, counting each minute once."""
    return sum(
        max(0, int((end - start).total_seconds() // 60)) for start, end in _merge(spans)
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
    merged = _merge(occupied)

    slots: list[Slot] = []
    cursor = window_start
    for start, end in merged:
        if start > cursor:
            slots.append(Slot(cursor, min(start, window_end)))
        cursor = max(cursor, end)
    if cursor < window_end:
        slots.append(Slot(cursor, window_end))
    return [slot for slot in slots if slot.minutes >= min_minutes]
