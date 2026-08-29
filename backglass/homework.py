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

from backglass import coursework, walkthrough
from backglass.config import Settings
from backglass.courses import subject_of
from backglass.ledger import USER_ID
from backglass.plan import capacity, timezones

#: Monday-first, matching `/schedule/week`. Two grids that disagree about where a week
#: starts are two grids the owner has to re-read every time they switch.
_WEEK_START = 0


def minutes_label(total: int | None) -> str:
    """`45m`, `2h`, `1h30`, or nothing at all.

    One formatter, because the to-do list prints a duration in three places — the row,
    the group header and the page total — and three spellings of two and a half hours on
    one page is a page that looks like it is quoting three different numbers.
    """
    if not total:
        return ""
    hours, rest = divmod(int(total), 60)
    if not hours:
        return f"{rest}m"
    return f"{hours}h" if not rest else f"{hours}h{rest:02d}"


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

    @property
    def is_class(self) -> bool:
        """A class meeting rather than something the owner has to remember.

        `8:00 CHM 113` on a Tuesday is the same `8:00 CHM 113` as every other Tuesday —
        the timetable, which `/classes` states once and `/schedule` draws in place. On a
        month of deadlines it is the row that repeats twenty times and decides nothing,
        so it is what the cell's fold collapses first.

        The test is the same regex the course filter runs (`courses.subject_of`), not a
        new classifier: a title carrying a course code the registrar issued is that
        course meeting. An advising appointment about CHM 113 would read as a class here
        and that is the right cost — one over-collapsed row inside a fold that opens.
        """
        return subject_of(self.title) is not None


@dataclass(frozen=True)
class Placement:
    """The day the planner actually put this work on, and how it went.

    The link the page exists to make. 2026-08-27, the owner: "other assignments havent
    been scheduled" — which was true, and unanswerable, because a due date and a plan
    lived on two surfaces that never referred to each other. A deadline is a claim about
    when work is owed; a block is a claim about when it happens. Only the second one gets
    the work done, and until now nothing said which obligations had one.
    """

    day: date
    outcome: str

    @property
    def label(self) -> str:
        return self.day.strftime("%-d %b")


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
    #: The day it is owed, always. `at` is the hour and is often absent — most of the
    #: ledger's deadlines are a date and no time — so anything that has to print a date
    #: outside the cell it was drawn in reads this instead, rather than an em-dash.
    day: date
    at: datetime | None
    minutes: int | None
    url: str
    source_item_id: int | None
    commitment_id: int | None
    overdue: bool = False
    done: bool = False
    #: Where the planner put it, if it put it anywhere. `None` is a real answer and the
    #: interesting one: work that is due and has no block is work nothing has made room
    #: for. An unlinked assignment can never have one — it has no commitment for a block
    #: to point at, which is a second reason the audit under the grid matters.
    placed: Placement | None = None
    #: How to start it, when the ledger can say anything about that — see
    #: `backglass/walkthrough.py`. `None` on a commitment with no Canvas row behind it,
    #: and a `Walkthrough` carrying a `reason` and no steps on work that is answered
    #: rather than worked through, which is the owner's own boundary and not a gap.
    walk: walkthrough.Walkthrough | None = None

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

    @property
    def short_title(self) -> str:
        """The title with the course code taken off the front of it.

        Every surface that prints a `Due` prints `course` beside it, so a Canvas title
        that opens with the same code says it twice and spends the width doing it —
        `CIS 236 · CIS 236: watch 1-3-2` clipped to `CIS 236 CIS 236: watch 1-…` in the
        month grid, where the half that identified the item was the half that got cut.
        Only a leading code is removed, and only the one already being shown: a title
        that mentions another course mid-sentence keeps it, because there the code is
        doing work.
        """
        title = self.title.strip()
        if not self.course:
            return title
        code = self.course.split(" (")[0]
        for prefix in (code, code.replace(" ", "")):
            if title.upper().startswith(prefix.upper()):
                rest = title[len(prefix) :].lstrip(" :–-—·")
                # Never strip down to nothing: a commitment whose whole text is the
                # course code is badly extracted, and an empty row hides that.
                if rest:
                    return rest
        return title

    @property
    def minutes_label(self) -> str:
        """`45m`, `2h`, `1h30` — or the empty string when nothing estimated it.

        Empty rather than a zero. An unestimated item is not a free one, and the list
        counts them separately underneath rather than adding nothing to the total and
        letting the day look lighter than it is.
        """
        return minutes_label(self.minutes)

    @property
    def rank(self) -> tuple[int, int, int, str, int, str]:
        """The order the to-do list is in, stated once so it can be read.

        Owner, 2026-08-29: *"ranks all homework by due date and time it takes"*. Due date
        is the spine — a deadline is the one thing about a piece of work that is not
        negotiable — and the estimate breaks ties within a day, longest first, because
        two things due Friday are not the same problem when one is four hours and the
        other is ten minutes and the four-hour one has to start first.

        In order: late before due, then the day, then a stated hour before a bare date
        (an 11:59pm deadline outranks "sometime Friday"), then the hour itself, then the
        longer estimate, then unestimated last — an item nothing sized is the one the
        owner most needs to look at rather than the one to start.

        **Overdue runs backwards, most recently missed first**, and that is deliberate.
        Sorted the obvious way the top of this list was a hall parking permit due 6 July
        — seven weeks dead, one of the 43 stale commitments the sidebar already flags —
        printed under the words "start here". Staleness is not urgency. A deadline missed
        yesterday is usually still recoverable and a deadline missed in July is a ledger
        hygiene problem, so the recoverable one goes on top. The 2026-08-28 lesson is the
        same shape: the actionable window is the one worth interrupting for.
        """
        # The day as an ordinal rather than its ISO string, so overdue can be reversed
        # with a minus: descending on a date needs arithmetic, and `-"2026-07-06"` is not
        # a thing. A negative here never meets a positive — the flag above separates them.
        when = self.day.toordinal()
        return (
            0 if self.overdue else 1,
            -when if self.overdue else when,
            0 if self.at is not None else 1,
            self.at.strftime("%H:%M") if self.at is not None else "",
            -(self.minutes or 0),
            self.title.lower(),
        )


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

    @property
    def event_summary(self) -> str:
        """`3 classes · 1 event` — the cell's calendar, in one line.

        Measured on the live ledger, 29 August: the month grid drew 159 event tiles
        against 160 due tiles, and the events were uncapped while the deadlines folded at
        six. A page whose docstring says deadlines are the subject and the calendar is
        context was rendering the opposite, and one Saturday carrying twelve class
        meetings set the height of its whole week row. So the events collapse to this
        line and open from it — owner's ruling, 2026-08-29, asked before it was built:
        fold them, do not drop them, because `capacity.calendar_events` staying the
        reader is what keeps this grid and `/schedule` from disagreeing about a Tuesday.
        """
        classes = sum(1 for event in self.events if event.is_class)
        rest = len(self.events) - classes
        parts = []
        if classes:
            parts.append(f"{classes} class{'' if classes == 1 else 'es'}")
        if rest:
            parts.append(f"{rest} event{'' if rest == 1 else 's'}")
        return " · ".join(parts)


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
    #: The course this month is filtered to, normalised — `""` when it is not filtered.
    course: str = ""
    #: The last day a live plan exists for. Every claim on this page about what is or is
    #: not scheduled stops here, and the page says so.
    planned_through: date | None = None
    #: Every course with something on this month, for the chip row. Read off the grid
    #: after filtering would give one entry, so it is built before the filter is applied.
    courses: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return self.first.strftime("%B %Y")

    @property
    def slug(self) -> str:
        """`chm113` — the anchor `classes.html` gives that course's card."""
        return self.course.replace(" ", "").lower()

    @property
    def unplanned(self) -> list[Due]:
        """Work that is due inside the planned horizon and that no live plan holds.

        Bounded by `planned_through`, and the bound is the whole point: past it the
        planner has not looked yet, so "no block" means nothing there. Open commitments
        only — a finished one needs no block, and an unlinked assignment cannot have one
        because there is no commitment for a block to point at, which the audit under the
        grid says in its own words.
        """
        if self.planned_through is None:
            return []
        return [
            item
            for week in self.weeks
            for day in week
            for item in day.due
            if day.in_month
            and day.day <= self.planned_through
            and item.kind == "commitment"
            and item.placed is None
        ]

    @property
    def planned_count(self) -> int:
        return sum(
            1
            for week in self.weeks
            for day in week
            for item in day.due
            if day.in_month and item.placed is not None
        )

    @property
    def empty(self) -> bool:
        """Nothing at all on the month as it is being shown, filters included."""
        return self.due_count == 0 and self.event_count == 0

    @property
    def due_count(self) -> int:
        return sum(len(day.due) for week in self.weeks for day in week if day.in_month)

    @property
    def event_count(self) -> int:
        return sum(len(day.events) for week in self.weeks for day in week if day.in_month)


@dataclass(frozen=True)
class Bucket:
    """One band of the to-do list: when the work in it is owed, and how much there is.

    The bands are the only thing the list adds to a flat sort, and they are worth adding
    because "due in four days" and "due in four weeks" are different kinds of fact even
    though they sort next to each other. Each carries its own load, because the number
    the owner actually acts on is not how many items this week holds but how many hours.
    """

    key: str
    label: str
    #: What the band means, in the owner's terms. Printed, not a comment: a band called
    #: "this week" that quietly meant "the next seven days" would be read as the calendar
    #: week it is not.
    note: str
    items: list[Due] = field(default_factory=list)

    @property
    def minutes(self) -> int:
        return sum(item.minutes or 0 for item in self.items)

    @property
    def load_label(self) -> str:
        return minutes_label(self.minutes)

    @property
    def unestimated(self) -> int:
        """How many rows contributed nothing to the total.

        Stated with the total, always. A band reading `2h30` that is really 2h30 plus
        nine unsized items is the windowed measurement of 2026-08-27 in a new place: a
        number describing where the counting stopped, read as a description of the work.
        """
        return sum(1 for item in self.items if not item.minutes)

    @property
    def unplanned(self) -> int:
        return sum(1 for item in self.items if item.placed is None)


@dataclass(frozen=True)
class Todo:
    """Everything owed, ranked, in bands — the Homework tab's own answer.

    Owner, 2026-08-29: *"i want a homework todo view that ranks all homework by due date
    and time it takes"*. The month grid answers "when is everything due" and is a shape,
    not an order; this answers "what do I do next", which is the question a student
    actually opens the tab with. Both read the same rows — nothing here is stored, and a
    row that appears on one appears on the other.

    Unbounded by month on purpose. A to-do list that stopped at the 31st would hide the
    thing due on the 2nd, which is exactly the work that needs starting now.
    """

    today: date
    buckets: list[Bucket]
    #: The course this list is filtered to, normalised — `""` when it is not filtered.
    course: str = ""
    only_coursework: bool = False
    courses: tuple[str, ...] = ()
    #: The last day a live plan exists for, so "no block" can be told from "not yet
    #: considered" — the same bound the month grid prints under its planner strip.
    planned_through: date | None = None
    #: How many open dated obligations the filters are keeping off this list. Printed,
    #: never merely applied: a page that quietly narrows what it is showing is a page
    #: whose count the owner will read as the whole ledger. The Homework tab opens on
    #: coursework because it is the Homework tab, and this number is how the owner learns
    #: there is a wider list and where the door to it is.
    filtered_out: int = 0

    @property
    def count(self) -> int:
        return sum(len(bucket.items) for bucket in self.buckets)

    @property
    def minutes(self) -> int:
        return sum(bucket.minutes for bucket in self.buckets)

    @property
    def load_label(self) -> str:
        return minutes_label(self.minutes)

    @property
    def unestimated(self) -> int:
        return sum(bucket.unestimated for bucket in self.buckets)

    @property
    def empty(self) -> bool:
        return self.count == 0

    @property
    def next_up(self) -> Due | None:
        """The single row at the top of the ranking, or None on an empty list.

        Printed above the bands, big. A list is a thing to scan and a next action is a
        thing to do, and the whole value of ranking is lost if the owner still has to
        work out which end to start.
        """
        for bucket in self.buckets:
            if bucket.items:
                return bucket.items[0]
        return None


#: The bands, in order, with the day each one runs to computed from today. Written as
#: data so the list's own shape is one thing to read rather than a chain of `elif`.
def _bands(today: date) -> list[tuple[str, str, str, date | None]]:
    """`(key, label, note, last day of the band)`. `None` is the open-ended tail."""
    # Sunday-ended, matching `/schedule/week` and the month grid's Monday-first weeks:
    # two surfaces that disagree about where a week ends make the owner re-read both.
    end_of_week = today + timedelta(days=6 - ((today.weekday() - _WEEK_START) % 7))
    return [
        (
            "overdue",
            "Overdue",
            "the day it was owed has already gone",
            today - timedelta(days=1),
        ),
        ("today", "Today", today.strftime("%A %-d %B"), today),
        (
            "tomorrow",
            "Tomorrow",
            (today + timedelta(days=1)).strftime("%A %-d %B"),
            today + timedelta(days=1),
        ),
        (
            "week",
            "The rest of this week",
            f"through Sunday {end_of_week.strftime('%-d %B')}",
            end_of_week,
        ),
        (
            "next",
            "Next week",
            f"the week of Monday {(end_of_week + timedelta(days=1)).strftime('%-d %B')}",
            end_of_week + timedelta(days=7),
        ),
        ("later", "Later", "everything after that, in the order it comes", None),
    ]


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


def subject(label: str) -> str:
    """`CHM 113` out of `CHM 113 (Lab)` — the course, without which shell of it.

    The filter is by course and never by component. A student asking "what does CHM 113
    want from me this month" means the lecture, the lab and the recitation; splitting
    them would hide two thirds of the answer behind a chip they did not know to click.
    """
    parsed = subject_of(label)
    return parsed[0] if parsed is not None else ""


def enrolled(conn: sqlite3.Connection) -> frozenset[str]:
    """Every course code the owner is actually taking, from Canvas and from the calendar.

    The guard on the sentence fallback below. `courses._CODE` is "two to four letters
    then three digits", which is what an ASU course code is made of and also what an ISBN
    and a room number are made of: `Buy Norton — ISBN 978-0-393…` produced a course called
    **ISBN 978**, and an AI meetup in room 151 produced **RM 151**. Both reached the chip
    row of the Homework page as classes the owner could filter to.

    A registrar-issued code is a fact the ledger already holds, so the fallback is checked
    against it rather than made stricter with a second regex — the next false positive
    will not be an ISBN, and a blocklist only ever knows about the one that already
    happened.

    **Both sources, because `courses.py` rules that the calendar decides a class exists
    and Canvas only decorates it** — LSB 191 is its own worked example, and this ledger
    carries a second in LIA 101, which meets on the calendar and has issued no Canvas
    assignment at all. Guarding on Canvas alone would strip the course off any commitment
    naming it, and the to-do list opens on coursework, so a stripped label is not a
    missing chip — it is the row vanishing from the tab that exists for it.
    """
    found = set()
    for row in conn.execute(
        "SELECT DISTINCT course FROM assignment WHERE user_id = ? AND course IS NOT NULL "
        "AND course != ''",
        (USER_ID,),
    ).fetchall():
        parsed = subject_of(str(row["course"]))
        if parsed is not None:
            found.add(parsed[0])
    for row in conn.execute(
        "SELECT DISTINCT title FROM source_item WHERE user_id = ? AND source LIKE 'calendar%' "
        "AND title IS NOT NULL AND title != ''",
        (USER_ID,),
    ).fetchall():
        parsed = subject_of(str(row["title"]))
        if parsed is not None:
            found.add(parsed[0])
    return frozenset(found)


def _course_label(course: str, what: str, known: frozenset[str] = frozenset()) -> str:
    """`CHM 113 (Lab)` from a Canvas course code, or from the commitment's own words.

    Falls back to the sentence because a commitment extracted from an email — "complete
    the CHM 113 safety quiz" — belongs to a course as surely as a Canvas row does, and
    the page that only labelled the Canvas ones would look like the email ones came from
    nowhere.

    The Canvas column is authoritative and the sentence is not, so only the sentence is
    checked against `enrolled` — see there for the ISBN it invented. An empty `known` set
    means the caller did not look the courses up, and the fallback then behaves as it
    always did rather than silently labelling nothing.
    """
    parsed = subject_of(course)
    if parsed is None:
        parsed = subject_of(what)
        if parsed is not None and known and parsed[0] not in known:
            return ""
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


def _planned_through(conn: sqlite3.Connection) -> date | None:
    """The last day a live plan exists for, or None if the planner has never run.

    The bound on every claim this page makes about scheduling. The planner proposes one
    day at a time, at 5:45 each morning, so on 27 August it has reached 8 September and
    everything after that is not unscheduled — it is unconsidered. Reporting "101 items
    unplanned" for a month the planner has not walked yet is the windowed-measurement
    failure of 2026-08-27 in a new place: a number that describes where the measurement
    stops and reads as a description of the work.
    """
    row = conn.execute(
        "SELECT MAX(local_date) AS last FROM day_plan "
        "WHERE user_id = ? AND status != 'superseded'",
        (USER_ID,),
    ).fetchone()
    try:
        return date.fromisoformat(str(row["last"])) if row and row["last"] else None
    except ValueError:
        return None


def _placements(conn: sqlite3.Connection) -> dict[int, Placement]:
    """Every commitment the live plans have a block for, and the first day it sits on.

    Not bounded to the grid, deliberately. A commitment due on the 16th may be planned
    for the 8th — that is the planner working, and a month page that only looked inside
    its own dates would report it unplanned. The whole plan table is 127 days and a
    handful of blocks each.

    `status != 'superseded'` is the same predicate `planner.current_plan_id` and the
    brief read a day's plan with: a replanned day leaves its old rows behind and counting
    them would report work as scheduled twice, on a day whose plan no longer exists.

    First day, when a commitment is split across several sittings. The question the page
    asks is "has anything made room for this", and the earliest block is the soonest
    honest answer to it.
    """
    rows = conn.execute(
        "SELECT b.commitment_id, d.local_date, b.outcome "
        "FROM plan_block b JOIN day_plan d ON d.id = b.day_plan_id "
        "WHERE d.user_id = ? AND d.status != 'superseded' "
        "  AND b.commitment_id IS NOT NULL "
        "ORDER BY d.local_date, b.starts_at",
        (USER_ID,),
    ).fetchall()
    out: dict[int, Placement] = {}
    for row in rows:
        key = int(row["commitment_id"])
        if key in out:
            continue
        try:
            out[key] = Placement(
                day=date.fromisoformat(str(row["local_date"])),
                outcome=str(row["outcome"] or "pending"),
            )
        except ValueError:
            continue
    return out


def load(
    conn: sqlite3.Connection,
    settings: Settings,
    first: date,
    *,
    today: date,
    now: datetime | None = None,
    only_coursework: bool = False,
    course: str | None = None,
) -> Month:
    """The month grid, filled.

    `now` is injected rather than read here so a test can pin overdue-ness without
    pinning the clock, and so the page and the tests agree about what "late" means.
    """
    weeks = _grid(first)
    grid_first, grid_last = weeks[0][0], weeks[-1][-1]
    tz = timezones.active_tz(settings, today)
    moment = now or timezones.local_now(settings)
    # Normalised through the same regex the label was built with, so `?course=chm113`
    # and `?course=CHM+113` are the one course they obviously are.
    wanted = subject(course or "")
    placements = _placements(conn)
    horizon = _planned_through(conn)
    known = enrolled(conn)

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
    #: Built as rows are read rather than off the finished grid, because the finished
    #: grid has been filtered to one course and would report only that one — leaving the
    #: chip row with no way back to the others.
    present: set[str] = set()
    for row in commitments:
        day, at = _local(str(row["due_at"]), tz)
        if day is None or not (grid_first <= day <= grid_last):
            continue
        item_id = int(row["source_item_id"])
        assignment = by_item.get(item_id)
        if assignment is not None:
            backed.add(item_id)
        course = _course_label(
            str(assignment["course"]) if assignment is not None else "",
            str(row["what"]),
            known,
        )
        if course:
            present.add(subject(course))
        if only_coursework and not course:
            continue
        if wanted and subject(course) != wanted:
            continue
        done = str(row["status"]) == "done"
        due.setdefault(day, []).append(
            Due(
                kind="done" if done else "commitment",
                title=str(row["what"]),
                course=course,
                day=day,
                at=at,
                minutes=row["estimated_minutes"],
                # The assignment's own page, not the calendar month the feed published.
                url=walkthrough.canvas_link(
                    str(assignment["url"] or ""), str(assignment["external_id"] or "")
                )
                if assignment is not None
                else "",
                source_item_id=item_id,
                commitment_id=int(row["id"]),
                overdue=not done and _is_late(day, at, today, moment),
                done=done,
                placed=placements.get(int(row["id"])),
            )
        )

    # The reconciliation. An assignment whose source item produced no commitment is
    # graded work that no other surface can show, so it is drawn here in its own
    # register and counted underneath.
    unlinked: list[Due] = []
    in_month = 0
    linked = 0
    for day, row in dated_assignments:
        item_id = row["source_item_id"]
        course = _course_label(str(row["course"] or ""), str(row["title"]), known)
        if course:
            present.add(subject(course))
        # The count is about what this page is showing. A month narrowed to CHM 113 that
        # went on reporting all 82 of the month's assignments would be answering a
        # question the owner had just navigated away from — and the unfiltered view still
        # carries the whole reconciliation, which is where the gap has to be visible.
        if wanted and subject(course) != wanted:
            continue
        counted = day.month == first.month and day.year == first.year
        if item_id is not None and int(item_id) in backed:
            if counted:
                in_month += 1
                linked += 1
            continue
        if counted:
            in_month += 1
        _, at = _local(str(row["due_at"]), tz)
        if only_coursework and not course:
            continue
        entry = Due(
            kind="unlinked",
            title=str(row["title"]),
            course=course,
            day=day,
            at=at,
            minutes=row["effort_minutes"],
            url=walkthrough.canvas_link(str(row["url"] or ""), str(row["external_id"] or "")),
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
                # A month filtered to one course is that course's whole month — its
                # lectures and labs as well as its deadlines. Showing every other
                # class's meetings beside one course's homework is the question nobody
                # asked.
                if not wanted or subject(event.title) == wanted
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

    return Month(
        first=first,
        weeks=filled,
        prev=shift(first, -1),
        next=shift(first, 1),
        today=today,
        counts=Counts(assignments=in_month, linked=linked, unlinked=in_month - linked),
        unlinked=unlinked,
        only_coursework=only_coursework,
        course=wanted,
        planned_through=horizon,
        courses=tuple(sorted(present)),
    )



def _materials(conn: sqlite3.Connection) -> dict[int, list[tuple[str, str, str]]]:
    """`assignment_material` rows by assignment, as `(kind, name, detail)`.

    One query for the page rather than one per row: the to-do list is a hundred items on
    a busy week and a per-row read is a hundred index lookups to build a fold most of
    them will never open.
    """
    out: dict[int, list[tuple[str, str, str]]] = {}
    rows = conn.execute(
        "SELECT assignment_id, kind, name, detail FROM assignment_material "
        "WHERE user_id = ? ORDER BY kind, name",
        (USER_ID,),
    ).fetchall()
    for row in rows:
        out.setdefault(int(row["assignment_id"]), []).append(
            (str(row["kind"]), str(row["name"]), str(row["detail"] or ""))
        )
    return out


def _walk(
    row: sqlite3.Row | None, materials: dict[int, list[tuple[str, str, str]]]
) -> walkthrough.Walkthrough | None:
    """The walkthrough for the assignment behind a row, or None when there is no row.

    A commitment read out of an email has no Canvas page to walk through and no
    description to read one out of; the source item behind it is where its evidence is,
    and that is already the row's own link.
    """
    if row is None:
        return None
    keys = row.keys()
    lock = None
    if "lock_at" in keys and row["lock_at"]:
        try:
            lock = datetime.fromisoformat(str(row["lock_at"]).replace("Z", "+00:00"))
        except ValueError:
            lock = None
    return walkthrough.build(
        title=str(row["title"]),
        description=str(row["description"] or "") if "description" in keys else "",
        kind=coursework.classify(
            str(row["title"]),
            str(row["description"] or "") if "description" in keys else "",
        ),
        url=str(row["url"] or ""),
        external_id=str(row["external_id"] or ""),
        points=row["points_possible"] if "points_possible" in keys else None,
        minutes=row["effort_minutes"] if "effort_minutes" in keys else None,
        effort_quote=str(row["effort_quote"] or "") if "effort_quote" in keys else "",
        materials=materials.get(int(row["id"]), []),
        lock_at=lock,
    )


def todo(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    today: date,
    now: datetime | None = None,
    only_coursework: bool = False,
    course: str | None = None,
) -> Todo:
    """Everything still owed, ranked by when it is due and how long it takes.

    The same two readers the month grid uses, unbounded and open-only:

    * open `i_owe` commitments with a due date, and
    * dated Canvas assignments that produced no commitment and are unsubmitted — the
      reconciliation gap the grid draws with a dashed outline. Leaving them off a to-do
      list would be the 2026-08-27 failure exactly: twenty-two pieces of graded work
      invisible on every surface because each surface only listed what some other one
      had already extracted.

    Finished work is not here. The grid keeps it, because a month looked back at with its
    finished work erased reads as a month where nothing happened; a to-do list is read
    forward, and a struck-through row on it is a row in the way.
    """
    tz = timezones.active_tz(settings, today)
    moment = now or timezones.local_now(settings)
    wanted = subject(course or "")
    known = enrolled(conn)
    placements = _placements(conn)
    horizon = _planned_through(conn)
    materials = _materials(conn)

    # Assignment rows by source item, so a commitment can borrow the course code, the
    # Canvas link and the effort estimate off the row behind it — and so the ones with
    # nothing behind them can be told apart afterwards.
    assignments = conn.execute(
        "SELECT * FROM assignment WHERE user_id = ? AND due_at IS NOT NULL "
        "AND submitted_at IS NULL ORDER BY due_at, id",
        (USER_ID,),
    ).fetchall()
    by_item: dict[int, sqlite3.Row] = {
        int(row["source_item_id"]): row
        for row in assignments
        if row["source_item_id"] is not None
    }

    commitments = conn.execute(
        "SELECT c.id, c.what, c.due_at, c.estimated_minutes, c.source_item_id "
        "FROM commitment c JOIN source_item si ON si.id = c.source_item_id "
        "WHERE c.user_id = ? AND c.direction = 'i_owe' AND c.due_at IS NOT NULL "
        "  AND c.status = 'open' "
        "ORDER BY c.due_at, c.id",
        (USER_ID,),
    ).fetchall()

    items: list[Due] = []
    backed: set[int] = set()
    #: Open, dated obligations the filters are keeping off the list. Counted rather than
    #: merely dropped — see `Todo.filtered_out`.
    hidden = 0
    #: The chip row, built before the filter is applied — read off the filtered list it
    #: would report the one course already selected and leave no way back to the others.
    present: set[str] = set()

    for row in commitments:
        day, at = _local(str(row["due_at"]), tz)
        if day is None:
            continue
        item_id = int(row["source_item_id"])
        assignment = by_item.get(item_id)
        if assignment is not None:
            backed.add(item_id)
        label = _course_label(
            str(assignment["course"]) if assignment is not None else "",
            str(row["what"]),
            known,
        )
        if label:
            present.add(subject(label))
        if (only_coursework and not label) or (wanted and subject(label) != wanted):
            hidden += 1
            continue
        items.append(
            Due(
                kind="commitment",
                title=str(row["what"]),
                course=label,
                day=day,
                at=at,
                # The commitment's own estimate first, the assignment's second: the
                # extraction read a sentence about this specific piece of work, and the
                # feed's `effort_minutes` is a default for the shape of it.
                minutes=row["estimated_minutes"]
                or (assignment["effort_minutes"] if assignment is not None else None),
                # The assignment's own Canvas page, not the calendar month the feed
                # published — see `walkthrough.canvas_link`. A commitment with no Canvas
                # row behind it keeps the empty string and falls back to its source item,
                # which is where its evidence is.
                url=walkthrough.canvas_link(
                    str(assignment["url"] or ""), str(assignment["external_id"] or "")
                )
                if assignment is not None
                else "",
                source_item_id=item_id,
                commitment_id=int(row["id"]),
                overdue=_is_late(day, at, today, moment),
                placed=placements.get(int(row["id"])),
                walk=_walk(assignment, materials),
            )
        )

    for row in assignments:
        item_id = row["source_item_id"]
        if item_id is not None and int(item_id) in backed:
            continue
        day, at = _local(str(row["due_at"]), tz)
        if day is None:
            continue
        label = _course_label(str(row["course"] or ""), str(row["title"]), known)
        if label:
            present.add(subject(label))
        if (only_coursework and not label) or (wanted and subject(label) != wanted):
            hidden += 1
            continue
        items.append(
            Due(
                kind="unlinked",
                title=str(row["title"]),
                course=label,
                day=day,
                at=at,
                minutes=row["effort_minutes"],
                url=walkthrough.canvas_link(
                    str(row["url"] or ""), str(row["external_id"] or "")
                ),
                source_item_id=int(item_id) if item_id is not None else None,
                commitment_id=None,
                overdue=_is_late(day, at, today, moment),
                walk=_walk(row, materials),
            )
        )

    items.sort(key=lambda item: item.rank)

    # Bucketed by walking the sorted list once. A band claims every remaining item up to
    # its last day, so an empty band between two full ones is a band with nothing in it
    # rather than a band that lost its rows to the one after.
    buckets: list[Bucket] = []
    remaining = list(items)
    for key, label, note, last in _bands(today):
        if last is None:
            taken, remaining = remaining, []
        else:
            taken = [item for item in remaining if item.day <= last]
            remaining = [item for item in remaining if item.day > last]
        if taken:
            buckets.append(Bucket(key=key, label=label, note=note, items=taken))

    return Todo(
        today=today,
        buckets=buckets,
        course=wanted,
        only_coursework=only_coursework,
        courses=tuple(sorted(present)),
        planned_through=horizon,
        filtered_out=hidden,
    )
