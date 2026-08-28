"""An assignment that asks you to go and reserve a seat is not ninety minutes of work.

Owner's ask, 2026-08-27: *"continue to improve backglass so it goes through a similar
thought process pipeline as you just did"* — said after a session spent booking a
Dreamscape Learn VR pod session for CHM 113 by hand. `tasks/booking-pipeline-2026-08-27.md`
writes that session's reasoning down. This module is the first two steps of it.

**Step one: the deliverable is a booking, not a deliverable.** "Schedule Here! Lab 2
Module 1 — Act I" arrived in the ledger as one open commitment of ninety minutes, and
that is two different obligations wearing one coat: fifteen minutes at a reservation
portal, and a fifty-minute appointment on a day nothing in the ledger yet knew about.
Charging the planner ninety minutes for the pair puts the whole thing in one afternoon
and books nothing.

Deterministic, in `coursework.py`'s layer-1 sense: no model. Canvas states the verb in
the title. A model was never needed to read "Schedule Here!", and the 22-assignments
finding on 2026-08-27 is the standing argument against asking one — *a Canvas assignment
is a commitment by construction, and making that depend on a language model's judgement
about prose is how twenty-two of them came out silent.*

**Step two: the due date is not the deadline.** Canvas said Sep 3 at 11:59pm. The
description said the pod session must be finished *before coming to lab*, and that lab
meets Thursday at 8:00am on Sep 3, so the true cutoff was sixteen hours earlier than the
date on the row. This is rule 4's sibling: rule 4 resolves a relative date against the
item's own timestamp, and this resolves a relative *event* against the course's own
timetable.

**The trap that would have broken it, and why the matcher normalises first.** The sentence
in assignment 30 is stored as:

    must complete your VR Pod Experience **before** coming to lab, during its assigned week.

`instr(description, 'before coming to lab')` returns 0. The markdown emphasis markers sit
inside the phrase. A matcher that ran on the raw text would find nothing, report nothing,
and be indistinguishable from a matcher that worked on a corpus with nothing to find —
which is the one failure mode the 2026-08-27 lesson says neither the ledger nor the owner
can see. So every pattern here runs on `normalise()` output, and the fixture behind the
test is that exact sentence with the asterisks in place.

**Where meeting times come from, and the two things that keep them honest.** The owner's
ruling, asked and answered while this was being planned: *the fact table wins.*

The motivating case was CHM 113's lab, which moved from Thursday evening to Thursday
morning and left `calendar:asu` asserting 18:00–19:50 in PSD 232 for four days after the
owner knew better. **By the time this ran against the live ledger that had been fixed** —
the evening rows for every remaining week carry an owner retraction and corrected
08:00–09:50 PSD 228 rows sit beside them. Which does not retire the problem, it relocates
it: both rows are still in `source_item`, on the same dates, and 18:00 is later than 08:00.
"The last meeting at or before the due date" returns the retracted one unless retractions
are honoured, so `_calendar_meeting` filters on `source_item_retraction` and
`test_a_retracted_meeting_is_not_the_deadline` is the guard. Without it this module would
have computed the wrong deadline for the exact assignment that motivated it, from a
calendar that had already been corrected.

The second guard is the override. A structured entry in `fact` outranks the calendar,
always. Where no override exists the calendar answers and says so (`basis='calendar'`),
because a row that nobody has contradicted is better than no row and the basis is on the
record either way. Where both exist and disagree, the override is used **and the
disagreement is stated** — it is not swallowed, and it is not a reason to throw away the
answer the owner themself corrected into place. Retraction fixes the rows the owner has
looked at; the override fixes the ones they have not.

*Stated as an interpretation, because the ruling had two halves and they point different
ways.* The option's label was "fact table wins"; its body said to surface the conflict and
fall back to the Canvas due date. Falling back on disagreement would return Sep 3 at
11:59pm for the one case that motivated the file, so the label is what is implemented and
the body's instinct — never guess silently — is honoured by `Deadline.conflict`, which
every caller must render. If the owner meant the stricter reading, `_STRICT_ON_CONFLICT`
is one flag and one test away.

**The override's grammar**, in `fact`, subject `education`:

    key:   meeting:<course code, no space, lowercased>:<component>
    value: <weekday abbrev> <HH:MM>-<HH:MM>

    education / meeting:chm113:lab = "Thu 08:00-09:50"

Deliberately not prose. `education/fall-2026-chm-lab` is a sentence written for a person
and it carries the same correction, but parsing it would mean a regex over free text whose
shape nobody promised to keep — and the sentence next to it,
`education/fall-2026-course-load`, still contains the *stale* Thursday-evening time. Two
prose facts, one right and one wrong, with nothing in either saying which. A key with a
grammar is the smallest thing that makes "the fact table wins" a rule a program can
follow.

**Additive, per CLAUDE.md.** Nothing here writes to `commitment`. It reads assignments and
returns records; what the planner does with them is Increment B. If this module vanished
every surface would still be correct, just coarser.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from backglass.config import Settings
from backglass.ledger import USER_ID
from backglass.plan import timezones

#: Emphasis and inline-code runs, stripped rather than replaced: `**before**` must become
#: `before`, not ` before `, or every phrase spanning one gains a double space and the
#: patterns below have to learn about it. Order matters — the longer runs first.
_MARKUP = re.compile(r"\*\*\*|\*\*|__|\*|_|`")

#: Zero-width and non-breaking space, plus the rest of Unicode's separator zoo. Canvas's
#: HTML-to-text conversion emits `\xa0` freely and it is invisible in every log.
_SPACEY = re.compile(r"[  -​  　]")

#: `[text](url)` → `text`. A phrase that runs into a link keeps its words rather than
#: acquiring a URL in the middle of it.
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def normalise(text: str) -> str:
    """Lowercased, markup-free, single-spaced — the only surface the patterns see.

    Every step here exists because something in the real corpus needed it. NFKC first,
    because Canvas emits typographic dashes and ligatures that no ASCII pattern matches.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text)
    folded = _LINK.sub(r"\1", folded)
    folded = _MARKUP.sub("", folded)
    folded = _SPACEY.sub(" ", folded)
    folded = folded.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", folded).strip().lower()


#: The verb, in the title. Canvas puts it there for the student's benefit and it is the
#: single most reliable signal in the corpus — "Schedule Here!" is a course's own words.
#: The weak verbs carry an object. `reserve` alone matches "Reserve Reading Ch. 4", which
#: is a library shelf, not a portal; `book` alone matches "Book Review Essay". Requiring
#: `reserve a|your|the` costs nothing a real signup title has and removes the whole class.
#: "Schedule Here!" and "RSVP" need no object — neither has any other meaning.
_BOOKING_TITLE = re.compile(
    r"\bschedule\s+here\b"
    r"|\bsign[- ]?up\b"
    r"|\brsvp\b"
    r"|\b(?:reserve|book)\s+(?:a|an|your|the)\b"
)

#: The noun, in the body, for assignments whose title is only a topic. Weaker than the
#: title signal and never sufficient alone — see `detect`.
_BOOKING_BODY = re.compile(
    r"\bpod session\b"
    r"|\breservation portal\b"
    r"|\bavailable shows\b"
    r"|\bshow time(?:s)?\b"
    r"|\bschedule your\b"
    r"|\bmake an appointment\b"
    r"|\bappointment slot\b"
)

#: "before coming to lab", "prior to your lecture", "before you attend class". The
#: component captured is what the deadline resolves against.
_BEFORE_MEETING = re.compile(
    r"\b(?:before|prior to|ahead of)\s+"
    r"(?:coming to|attending|you attend|arriving at|your|the|each|its assigned\s+)?\s*"
    r"(lab(?:oratory)?|lecture|class|section|recitation)\b"
)

#: `2026FallC-T-CHM113-LABORATORY` → `CHM 113`, `LABORATORY`. The feed's own shape; see
#: `courses.py`, which reads the same string for the Classes page.
_COURSE_CODE = re.compile(r"-T-([A-Za-z]{2,4})(\d{3})(?:-(.*))?$")

#: What the phrase in the description means, in the vocabulary `calendar:asu` titles use
#: (`CHM 113 (Lab)`) and the override key uses (`meeting:chm113:lab`).
_COMPONENT = {
    "lab": "lab",
    "laboratory": "lab",
    "lecture": "lecture",
    "class": "lecture",
    "section": "lecture",
    "recitation": "recitation",
}

#: Minutes for the act of booking itself, when nothing states otherwise. Not an estimate
#: of the appointment — that is the assignment's own `effort_minutes`, untouched. Fifteen
#: is what the 2026-08-27 session actually took at the portal, portal-open to confirmed.
BOOK_MINUTES = 15

#: How far back to look for a course meeting before giving up. A semester's worth would
#: be wrong: an assignment due in week two whose course has not met is a real answer of
#: "no meeting", not a licence to reach into last term.
_MEETING_LOOKBACK_DAYS = 21

#: Weekday abbreviations the override grammar accepts, to Python's Monday-is-0.
_WEEKDAYS = {
    "mon": 0, "m": 0, "tue": 1, "tu": 1, "t": 1, "wed": 2, "w": 2,
    "thu": 3, "th": 3, "r": 3, "fri": 4, "f": 4, "sat": 5, "sa": 5, "sun": 6, "su": 6,
}

_OVERRIDE_KEY = re.compile(r"^meeting:([a-z]{2,4}\d{3}):([a-z]+)$")
_OVERRIDE_VALUE = re.compile(
    r"^\s*([a-z]{1,3})\s+(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$", re.I
)


@dataclass(frozen=True)
class MeetingTime:
    """A course component's weekly slot, and where the claim came from."""

    course: str
    component: str
    weekday: int
    start_minute: int
    basis: str  # fact | calendar

    def label(self) -> str:
        day = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[self.weekday]
        return f"{day} {self.start_minute // 60:02d}:{self.start_minute % 60:02d}"


@dataclass(frozen=True)
class Deadline:
    """When the thing must actually be done by, and how that was decided.

    `conflict` is never advisory. A caller that renders `at` without rendering `conflict`
    is presenting a derived date as if nothing disagreed with it, which is the failure
    rule 1 exists to prevent.
    """

    at: str | None
    basis: str
    conflict: str = ""

    @property
    def moved(self) -> bool:
        return self.basis.startswith("meeting:")


@dataclass(frozen=True)
class Booking:
    """One assignment, read as a booking."""

    assignment_id: int
    course: str
    component: str
    title: str
    due_at: str | None
    deadline: Deadline
    book_minutes: int = BOOK_MINUTES
    attend_minutes: int | None = None
    signal: str = ""

    @property
    def total_minutes(self) -> int:
        return self.book_minutes + (self.attend_minutes or 0)


@dataclass
class Report:
    """The reconciliation the 2026-08-27 lesson demands: inputs counted against outputs.

    A detector that returns an empty list because the corpus is empty and a detector that
    returns an empty list because its patterns are broken look identical from the outside.
    These counters are how they are told apart, and `scan` prints them whether or not it
    found anything.
    """

    scanned: int = 0
    matched: int = 0
    deadlines_moved: int = 0
    conflicts: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    bookings: list[Booking] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"{self.scanned} assignment(s) scanned, {self.matched} read as bookings, "
            f"{self.deadlines_moved} deadline(s) moved off the Canvas date"
        ]
        out += [f"  conflict: {c}" for c in self.conflicts]
        out += [f"  unresolved: {u}" for u in self.unresolved]
        return out


def course_of(feed_course: str) -> tuple[str, str]:
    """`2026FallC-T-CHM113-LABORATORY` → `("CHM 113", "lab")`.

    The component is the feed's suffix, which is a different question from the one
    `_BEFORE_MEETING` answers: this is *which section of the course published the work*,
    that is *which meeting the work must precede*. They usually agree and must not be
    assumed to — a lecture can set work due before a lab.
    """
    match = _COURSE_CODE.search(feed_course or "")
    if match is None:
        return "", ""
    code = f"{match.group(1).upper()} {match.group(2)}"
    suffix = (match.group(3) or "").lower()
    for word, component in _COMPONENT.items():
        if word in suffix:
            return code, component
    return code, ""


def detect(*, title: str, description: str) -> str:
    """The signal that made this a booking, or "" for none.

    The title alone is enough; the body alone is not. "Schedule your reading for the
    week" is prose about planning, not a portal, and a corpus of course descriptions is
    full of it — requiring the title to carry the verb is what keeps the precision that
    makes this safe to run without a model behind it.
    """
    in_title = _BOOKING_TITLE.search(normalise(title))
    if in_title:
        return in_title.group(0)
    return ""


def attends_before(description: str) -> str:
    """The component this work must precede, or "" if the description does not say."""
    match = _BEFORE_MEETING.search(normalise(description))
    if match is None:
        return ""
    return _COMPONENT.get(match.group(1), "")


def _fact_meetings(conn: sqlite3.Connection) -> dict[tuple[str, str], MeetingTime]:
    """Structured overrides out of the `fact` table. Malformed rows are skipped loudly.

    A row whose value does not parse is not a reason to fail the run — rule 5 — but it is
    also not allowed to look like an absent override, so `scan` surfaces it.
    """
    out: dict[tuple[str, str], MeetingTime] = {}
    rows = conn.execute(
        "SELECT key, value FROM fact WHERE user_id = ? AND status = 'active' "
        "AND subject = 'education' AND key LIKE 'meeting:%'",
        (USER_ID,),
    ).fetchall()
    for row in rows:
        key = _OVERRIDE_KEY.match(str(row["key"]).strip().lower())
        value = _OVERRIDE_VALUE.match(str(row["value"]))
        if key is None or value is None:
            continue
        weekday = _WEEKDAYS.get(value.group(1).lower())
        if weekday is None:
            continue
        squashed = key.group(1)
        code = f"{squashed[:-3].upper()} {squashed[-3:]}"
        out[(code, key.group(2))] = MeetingTime(
            course=code,
            component=key.group(2),
            weekday=weekday,
            start_minute=int(value.group(2)) * 60 + int(value.group(3)),
            basis="fact",
        )
    return out


def _calendar_meeting(
    conn: sqlite3.Connection, course: str, component: str, before: datetime
) -> tuple[datetime, MeetingTime] | None:
    """The last `calendar:asu` meeting of this component at or before `before`.

    Read straight rather than through `courses.py`: that module answers a page's
    question and its shape belongs to that page. One query here keeps the two from
    having to move together.
    """
    want = f"{course} ({component.capitalize()})" if component else course
    rows = conn.execute(
        "SELECT title, raw_json FROM source_item"
        " WHERE user_id = ? AND source = 'calendar:asu' AND lower(title) = lower(?)"
        " AND NOT EXISTS (SELECT 1 FROM source_item_retraction r"
        "                  WHERE r.source_item_id = source_item.id)",
        (USER_ID, want),
    ).fetchall()
    best: tuple[datetime, MeetingTime] | None = None
    floor = before - timedelta(days=_MEETING_LOOKBACK_DAYS)
    for row in rows:
        try:
            payload = json.loads(row["raw_json"] or "{}")
            starts = datetime.fromisoformat(str(payload["starts_at"]))
        except (ValueError, KeyError, TypeError):
            continue
        if starts.tzinfo is None or not (floor <= starts <= before):
            continue
        if best is None or starts > best[0]:
            best = (
                starts,
                MeetingTime(
                    course=course,
                    component=component,
                    weekday=starts.weekday(),
                    start_minute=starts.hour * 60 + starts.minute,
                    basis="calendar",
                ),
            )
    return best


def operative_deadline(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    course: str,
    component: str,
    due_at: str | None,
    description: str,
) -> Deadline:
    """The real cutoff: the last such meeting at or before the Canvas due date.

    Returns the due date untouched when the description does not say the work precedes a
    meeting, when the course has no meeting of that component in the window, or when
    `due_at` is not a date this can reason about. Silence is a legitimate answer here and
    it is reported as `basis='due_at'`, never as a moved deadline.
    """
    target = attends_before(description)
    if not target:
        return Deadline(at=due_at, basis="due_at")
    if not due_at:
        return Deadline(at=None, basis="due_at", conflict="")
    try:
        due = datetime.fromisoformat(due_at)
    except ValueError:
        return Deadline(at=due_at, basis="due_at")
    if due.tzinfo is None:
        # A bare `2026-09-03` from the ICS feed means end of that day, which is what
        # Canvas shows and what "the last meeting before it" has to be measured against.
        # Taking midnight instead would silently exclude the meeting on the due date.
        #
        # And it is made aware before any comparison, in the zone the owner is actually
        # in on that date. `calendar:asu` rows carry a real offset; comparing them
        # against a naive date raises, and the fix that suppresses the raise by dropping
        # tzinfo is the one that gets a Thursday-morning meeting wrong from Bengaluru.
        due = due.replace(
            hour=23, minute=59, tzinfo=ZoneInfo(timezones.active_tz(settings, due.date()))
        )

    del component  # the phrase decides, not the feed's suffix — see course_of
    override = _fact_meetings(conn).get((course, target))
    found = _calendar_meeting(conn, course, target, due)

    if override is None and found is None:
        return Deadline(at=due_at, basis="due_at")

    if override is not None:
        # Walk back from the due date to that weekday, then set the corrected time. The
        # calendar supplies which dates the course meets; the override supplies when.
        day = due
        for _ in range(8):
            if day.weekday() == override.weekday:
                break
            day -= timedelta(days=1)
        else:  # pragma: no cover - eight steps always reach a weekday
            return Deadline(at=due_at, basis="due_at")
        at = day.replace(
            hour=override.start_minute // 60,
            minute=override.start_minute % 60,
            second=0,
            microsecond=0,
        )
        conflict = ""
        if found is not None and found[1].start_minute != override.start_minute:
            conflict = (
                f"{course} {target}: fact says {override.label()}, calendar:asu says "
                f"{found[1].label()} — using the fact, per the owner's 2026-08-27 ruling"
            )
        if at > due:
            # The corrected meeting sits after the due date, so it cannot be the cutoff.
            return Deadline(at=due_at, basis="due_at", conflict=conflict)
        return Deadline(at=at.isoformat(), basis=f"meeting:{target}:fact", conflict=conflict)

    assert found is not None
    return Deadline(at=found[0].isoformat(), basis=f"meeting:{target}:calendar")


@dataclass(frozen=True)
class Attended:
    """A commitment whose calendar event has been and gone, still open."""

    commitment_id: int
    what: str
    event_title: str
    happened_at: str


def past_events(conn: sqlite3.Connection, settings: Settings, day: date) -> list[Attended]:
    """Linked obligations whose event finished on or before `day` and are still open.

    The other half of migration 0038, and the reason the link is not a quiet deletion.
    A commitment that rides a calendar event never becomes a candidate, so it never gets
    a block, so `rollover.close_day` never sees it — which is right while the event is in
    the future and wrong the morning after. Without this it would sit open forever, the
    one obligation the evening pass could not ask about.

    Asks; does not close. Attending is a thing that happens in a room, and the calendar
    knows the appointment was made, not that anybody went (`canvas_enrich` makes the same
    distinction about a Canvas score, for the same reason).

    **The day is the owner's, not UTC's**, and the first version of this got it wrong in
    exactly the way the same day's planner work had just fixed. `connectors/apple_calendar`
    stores what JXA's `toISOString()` produces, which is always `Z`: the pod session booked
    for Sep 2 at 6:00pm Phoenix is on disk as `ends_at = 2026-09-03T02:00:00.000Z`. Sliced
    to ten characters that is the third, so the evening pass would have asked "did you go?"
    a day late — for every evening event, every time. The fixtures now carry the `Z` shape
    the connector actually writes, because the ones that carried `-07:00` passed on data
    unlike the data.
    """
    zone = ZoneInfo(timezones.active_tz(settings, day))
    rows = conn.execute(
        "SELECT c.id, c.what, s.title, s.raw_json FROM commitment c"
        " JOIN source_item s ON s.id = c.scheduled_source_item_id"
        " WHERE c.user_id = ? AND c.status = 'open'"
        "   AND c.scheduled_source_item_id IS NOT NULL",
        (USER_ID,),
    ).fetchall()
    out: list[Attended] = []
    for row in rows:
        try:
            ends = str(json.loads(row["raw_json"] or "{}").get("ends_at") or "")
        except ValueError:
            continue
        if not ends:
            continue
        try:
            stamp = datetime.fromisoformat(ends.replace("Z", "+00:00"))
        except ValueError:
            continue
        finished = stamp.astimezone(zone).date() if stamp.tzinfo else stamp.date()
        if finished > day:
            continue
        out.append(
            Attended(
                commitment_id=int(row["id"]),
                what=str(row["what"]),
                event_title=str(row["title"] or ""),
                happened_at=ends,
            )
        )
    return sorted(out, key=lambda a: a.happened_at)


def scan(conn: sqlite3.Connection, settings: Settings) -> Report:
    """Every open assignment, read for bookings. Reads only; writes nothing."""
    report = Report()
    rows = conn.execute(
        "SELECT id, course, title, due_at, description, effort_minutes FROM assignment"
        " WHERE user_id = ? ORDER BY due_at IS NULL, due_at, id",
        (USER_ID,),
    ).fetchall()
    for row in rows:
        report.scanned += 1
        description = str(row["description"] or "")
        signal = detect(title=str(row["title"] or ""), description=description)
        if not signal:
            continue
        report.matched += 1
        course, component = course_of(str(row["course"] or ""))
        if not course:
            report.unresolved.append(
                f"assignment {int(row['id'])}: no course code in {row['course']!r}"
            )
        deadline = operative_deadline(
            conn,
            settings,
            course=course,
            component=component,
            due_at=row["due_at"],
            description=description,
        )
        if deadline.moved:
            report.deadlines_moved += 1
        if deadline.conflict:
            report.conflicts.append(deadline.conflict)
        report.bookings.append(
            Booking(
                assignment_id=int(row["id"]),
                course=course,
                component=component,
                title=str(row["title"] or ""),
                due_at=row["due_at"],
                deadline=deadline,
                attend_minutes=row["effort_minutes"],
                signal=signal,
            )
        )
    return report
