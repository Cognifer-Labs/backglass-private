"""The month, as a student reads it: what is due on each day, and what is already on it.

A third register beside the day and week timelines. `/schedule` answers "what am I doing
in the next hour" and draws durations; this answers "when is everything due" and draws
dates. A wider week would not have done — the question is month-shaped, because a
syllabus is.

Nothing here is stored. Two readers over tables that already exist:

* **due** — `commitment` rows the owner owes, joined through `source_item` to the
  `assignment` row behind them where there is one, for the course code, the Canvas link
  and the effort estimate.
* **on** — `capacity.calendar_events`, minus routines and walks. The same reader the
  planner and the schedule page build on, so the calendar cannot disagree with the day
  plan about what a Tuesday holds.

And one thing neither of those is: the **reconciliation**. An assignment with a due date
and nothing extracted from it is invisible on every other surface — 22 of them were, on
2026-08-27, and the lesson written that day says to count input rows against output rows
because "silence is the one failure mode neither the ledger nor the owner can see". So
the page counts assignments due in the month against the commitments backing them, draws
the difference on the grid in its own register, and names it underneath. A page that
quietly left an unlinked assignment off would be repeating the failure it exists to
report.
"""

from __future__ import annotations

import calendar as _calendar
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from backglass.config import Settings
from backglass.courses import subject_of
from backglass.ledger import USER_ID
from backglass.plan import capacity, timezones

#: Monday-first, matching `/schedule/week`. Two grids that disagree about where a week
#: starts are two grids the owner has to re-read every time they switch.
_WEEK_START = 0


@dataclass(frozen=True)
class Event:
    """Something the day already holds at a stated hour."""

    title: str
    starts_at: datetime
    ends_at: datetime
    location: str = ""
    source_item_id: int | None = None
    allday: bool = False

    @property
    def time_label(self) -> str:
        if self.allday:
            return "all day"
        return self.starts_at.strftime("%-I:%M").lower()

    @property
    def span_label(self) -> str:
        if self.allday:
            return "all day"
        start = self.starts_at.strftime("%-I:%M%p").lower().replace(":00", "")
        end = self.ends_at.strftime("%-I:%M%p").lower().replace(":00", "")
        return f"{start}–{end}"


@dataclass(frozen=True)
class Due:
    """One thing owed on a day, and the row it can be checked against.

    `kind` is the register it is drawn in, and the three are genuinely different claims:
    a `commitment` is something the ledger extracted and can show a source sentence for;
    an `assignment` is a Canvas row with a deadline that nothing was extracted from, so
    it is real work with no commitment behind it; `done` is either of those, resolved.
    """

    kind: str
    title: str
    course: str
    at: datetime | None
    minutes: int | None
    url: str
    source_item_id: int | None
    commitment_id: int | None
    overdue: bool = False
    done: bool = False

    @property
    def time_label(self) -> str:
        """The hour it is due, or the empty string when the source never named one.

        Empty rather than a made-up midnight: "by Friday" in an email said Friday, and
        printing 12:00am would be the page inventing a deadline the owner never got.
        """
        if self.at is None:
            return ""
        return self.at.strftime("%-I:%M%p").lower().replace(":00", "")

    @property
    def href(self) -> str:
        """Where the owner goes to check this claim. Rule 1, as a link.

        The Canvas URL when the assignment carried one — that is the page the work is
        actually done on — and otherwise the raw source item the commitment was read out
        of, which is the evidence behind the sentence.
        """
        if self.url:
            return self.url
        if self.source_item_id is not None:
            return f"/source/{self.source_item_id}"
        return ""


@dataclass(frozen=True)
class Day:
    day: date
    in_month: bool
    is_today: bool
    past: bool
    events: list[Event] = field(default_factory=list)
    due: list[Due] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.events and not self.due

    @property
    def load_minutes(self) -> int:
        return sum(item.minutes or 0 for item in self.due if not item.done)


@dataclass(frozen=True)
class Counts:
    """The reconciliation, as three numbers that have to add up.

    `assignments` is the input row count and `linked + unlinked` is the output row count.
    They are printed together on purpose: a derived surface that reports only its output
    cannot tell you what it dropped.
    """

    assignments: int
    linked: int
    unlinked: int


@dataclass(frozen=True)
class Month:
    first: date
    weeks: list[list[Day]]
    prev: date
    next: date
    today: date
    counts: Counts
    unlinked: list[Due]
    only_coursework: bool

    @property
    def label(self) -> str:
        return self.first.strftime("%B %Y")

    @property
    def due_count(self) -> int:
        return sum(len(day.due) for week in self.weeks for day in week if day.in_month)

    @property
    def event_count(self) -> int:
        return sum(len(day.events) for week in self.weeks for day in week if day.in_month)


def month_of(day: date) -> date:
    return day.replace(day=1)


def shift(first: date, months: int) -> date:
    """The first of the month `months` away. Written out rather than reached for with a
    library, because the whole of it is two lines of integer arithmetic."""
    index = (first.year * 12 + first.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def _grid(first: date) -> list[list[date]]:
    start = first - timedelta(days=(first.weekday() - _WEEK_START) % 7)
    last = first.replace(day=_calendar.monthrange(first.year, first.month)[1])
    end = last + timedelta(days=6 - ((last.weekday() - _WEEK_START) % 7))
    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    return [days[n : n + 7] for n in range(0, len(days), 7)]


def _local(value: str, tz: str) -> tuple[date | None, datetime | None]:
    """`(day, instant)` for a stored due date, in the owner's zone for that day.

    Three shapes reach this, and conflating them is how a deadline moves. `2026-09-02` is
    a day and no hour — the extraction read "by Tuesday" and there was no time to read.
    `2026-09-02T23:59:00` is a stated local hour. `2026-09-03T06:59:59Z` is the same
    instant written in UTC by a feed, and bucketing it by its own string would file a
    Tuesday-night deadline under Wednesday, which is the bug that would make this whole
    page lie by one day for every Canvas row on it.
    """
    text = (value or "").strip()
    if not text:
        return None, None
    if len(text) == 10:
        try:
            return date.fromisoformat(text), None
        except ValueError:
            return None, None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    if moment.tzinfo is None:
        # A stored local wall time: the extraction read "23:59" off a source and rule 4
        # resolved it against that source's own day. Stamping the owner's zone on it is
        # what it already meant — and leaving it naive makes every overdue comparison on
        # the page raise, because "now" is aware.
        return moment.date(), moment.replace(tzinfo=ZoneInfo(tz))
    local = moment.astimezone(ZoneInfo(tz))
    return local.date(), local


def _is_late(day: date, at: datetime | None, today: date, moment: datetime) -> bool:
    """Late, for both shapes of due date.

    Most of the ledger's deadlines carry no hour — an assignment feed's all-day row, an
    email's "by Friday" — so a lateness test that only compares instants silently never
    fires for the commonest shape, on the one page where lateness is the whole point. A
    dated-but-untimed item is late once its day is behind: it is due by the end of that
    day, so today's is not late yet, which is the same reading `_local` refuses to
    launder into a midnight.
    """
    return at < moment if at is not None else day < today


def _due_order(item: Due) -> tuple[int, int, str]:
    """Sort inside one cell: unfinished before finished, timed before untimed, then hour."""
    return (
        1 if item.done else 0,
        0 if item.at is not None else 1,
        item.at.strftime("%H:%M") if item.at is not None else "",
    )


def _course_label(course: str, what: str) -> str:
    """`CHM 113 (Lab)` from a Canvas course code, or from the commitment's own words.

    Falls back to the sentence because a commitment extracted from an email — "complete
    the CHM 113 safety quiz" — belongs to a course as surely as a Canvas row does, and
    the page that only labelled the Canvas ones would look like the email ones came from
    nowhere.
    """
    parsed = subject_of(course) or subject_of(what)
    if parsed is None:
        return ""
    subject, component = parsed
    return subject if component == "Lecture" else f"{subject} ({component})"


def _assignments(
    conn: sqlite3.Connection, tz: str, first_day: date, last_day: date
) -> tuple[dict[int, sqlite3.Row], list[tuple[date, sqlite3.Row]]]:
    """Assignment rows falling inside the grid, by source item and by day.

    Read over a widened string range and bucketed in Python rather than filtered in SQL:
    `due_at` holds three shapes (see `_local`) and a SQL `BETWEEN` over them compares the
    strings, which is only accidentally the same question.
    """
    rows = conn.execute(
        "SELECT * FROM assignment WHERE user_id = ? AND due_at IS NOT NULL "
        "AND due_at >= ? AND due_at < ? ORDER BY due_at, id",
        (
            USER_ID,
            (first_day - timedelta(days=2)).isoformat(),
            (last_day + timedelta(days=2)).isoformat() + "T99",
        ),
    ).fetchall()
    by_item: dict[int, sqlite3.Row] = {}
    dated: list[tuple[date, sqlite3.Row]] = []
    for row in rows:
        day, _ = _local(str(row["due_at"]), tz)
        if day is None or not (first_day <= day <= last_day):
            continue
        if row["source_item_id"] is not None:
            by_item[int(row["source_item_id"])] = row
        dated.append((day, row))
    return by_item, dated


def load(
    conn: sqlite3.Connection,
    settings: Settings,
    first: date,
    *,
    today: date,
    now: datetime | None = None,
    only_coursework: bool = False,
) -> Month:
    """The month grid, filled.

    `now` is injected rather than read here so a test can pin overdue-ness without
    pinning the clock, and so the page and the tests agree about what "late" means.
    """
    weeks = _grid(first)
    grid_first, grid_last = weeks[0][0], weeks[-1][-1]
    tz = timezones.active_tz(settings, today)
    moment = now or timezones.local_now(settings)

    by_item, dated_assignments = _assignments(conn, tz, grid_first, grid_last)

    # Every commitment the owner owes with a date on it. `done` rows are kept, and only
    # inside the grid: a month with its finished work erased reads as a month where
    # nothing happened, and the point of looking back at September in October is to see
    # that it did.
    commitments = conn.execute(
        "SELECT c.id, c.what, c.due_at, c.estimated_minutes, c.status, c.source_item_id, "
        "       si.source "
        "FROM commitment c JOIN source_item si ON si.id = c.source_item_id "
        "WHERE c.user_id = ? AND c.direction = 'i_owe' AND c.due_at IS NOT NULL "
        "  AND c.status IN ('open', 'done') "
        "  AND c.due_at >= ? AND c.due_at < ? "
        "ORDER BY c.due_at, c.id",
        (
            USER_ID,
            (grid_first - timedelta(days=2)).isoformat(),
            (grid_last + timedelta(days=2)).isoformat() + "T99",
        ),
    ).fetchall()

    due: dict[date, list[Due]] = {}
    backed: set[int] = set()
    for row in commitments:
        day, at = _local(str(row["due_at"]), tz)
        if day is None or not (grid_first <= day <= grid_last):
            continue
        item_id = int(row["source_item_id"])
        assignment = by_item.get(item_id)
        if assignment is not None:
            backed.add(item_id)
        course = _course_label(
            str(assignment["course"]) if assignment is not None else "", str(row["what"])
        )
        if only_coursework and not course:
            continue
        done = str(row["status"]) == "done"
        due.setdefault(day, []).append(
            Due(
                kind="done" if done else "commitment",
                title=str(row["what"]),
                course=course,
                at=at,
                minutes=row["estimated_minutes"],
                url=str(assignment["url"] or "") if assignment is not None else "",
                source_item_id=item_id,
                commitment_id=int(row["id"]),
                overdue=not done and _is_late(day, at, today, moment),
                done=done,
            )
        )

    # The reconciliation. An assignment whose source item produced no commitment is
    # graded work that no other surface can show, so it is drawn here in its own
    # register and counted underneath.
    unlinked: list[Due] = []
    in_month = 0
    for day, row in dated_assignments:
        item_id = row["source_item_id"]
        if item_id is not None and int(item_id) in backed:
            if day.month == first.month and day.year == first.year:
                in_month += 1
            continue
        if day.month == first.month and day.year == first.year:
            in_month += 1
        _, at = _local(str(row["due_at"]), tz)
        course = _course_label(str(row["course"] or ""), str(row["title"]))
        if only_coursework and not course:
            continue
        entry = Due(
            kind="unlinked",
            title=str(row["title"]),
            course=course,
            at=at,
            minutes=row["effort_minutes"],
            url=str(row["url"] or ""),
            source_item_id=int(item_id) if item_id is not None else None,
            commitment_id=None,
            overdue=not row["submitted_at"] and _is_late(day, at, today, moment),
            done=row["submitted_at"] is not None,
        )
        due.setdefault(day, []).append(entry)
        if day.month == first.month and day.year == first.year:
            unlinked.append(entry)

    # Calendar rows and confirmed plans only. A routine is configuration and a walk is
    # the cost of an event — neither is a thing the owner has to remember, which is what
    # a month grid is for. Read for the whole grid at once: per-day it is one full scan
    # of `source_item` per cell, which is 14ms forty-two times over.
    calendar = capacity.calendar_events_between(conn, settings, grid_first, grid_last)

    filled: list[list[Day]] = []
    for week in weeks:
        row_days: list[Day] = []
        for day in week:
            events = [
                Event(
                    title=event.title,
                    starts_at=event.starts_at,
                    ends_at=event.ends_at,
                    location=event.location,
                    source_item_id=event.source_item_id,
                    allday=event.allday,
                )
                for event in calendar.get(day, [])
            ]
            row_days.append(
                Day(
                    day=day,
                    in_month=day.month == first.month and day.year == first.year,
                    is_today=day == today,
                    past=day < today,
                    events=events,
                    # Undated-within-the-day last, finished last of all: the cell is read
                    # top-down as "what is on me today", and a struck-through row at the
                    # top of it is the least useful line on the page.
                    due=sorted(due.get(day, []), key=_due_order),
                )
            )
        filled.append(row_days)

    linked = sum(1 for day, row in dated_assignments
                 if row["source_item_id"] is not None
                 and int(row["source_item_id"]) in backed
                 and day.month == first.month and day.year == first.year)
    return Month(
        first=first,
        weeks=filled,
        prev=shift(first, -1),
        next=shift(first, 1),
        today=today,
        counts=Counts(assignments=in_month, linked=linked, unlinked=in_month - linked),
        unlinked=unlinked,
        only_coursework=only_coursework,
    )
