"""A semester, read out of the ledger: what each class is, when it meets, what it wants.

The complaint this answers, 2026-08-21: the ledger knew 159 Canvas assignments, the
calendar knew seven rooms and seven instructors, the drop folder held the syllabi, and
nothing put them beside each other. "What is CHM 113 and what does it want from me this
week" took three surfaces and a browser tab.

**No new table.** Every fact here already exists somewhere:

  - meetings, rooms and instructors — `calendar:asu` rows, whose `raw_json` carries
    `course`, `location`, `instructors` and the event's own offsets;
  - coursework — the `assignment` table and its `assignment_material` rows;
  - exams — `calendar:apple` rows written to the owner's "ASU Fall 2026" calendar,
    which is where the syllabus-derived dates live. Only the timed ones: `apple_calendar`
    drops all-day events at ingest on purpose (docs/07 — an all-day row is not capacity),
    so the drop deadline, the no-class days and the VR booking reminders are on the
    owner's calendar and deliberately not in the ledger. The page says so rather than
    pretending the semester has no deadlines;
  - obligations — open `commitment` rows, which is how a syllabus read through the drop
    folder becomes a date this page can show with its evidence attached;
  - documents — `files` rows, once the drop folder points at the course archive.

A migration would be live on the scheduler's clock inside thirty minutes (the 2026-08-21
lesson), and none is needed: this is a reader, and if it vanished every other surface
would still be correct.

**How the three vocabularies are joined.** Canvas names a course
`2026FallC-T-CHM113-60105`, the calendar names it `CHM 113 (Lab)`, and the archive names
its folder `CHM113-Lab`. All three carry the same subject and number, so `_subject`
reduces each to `("CHM 113", "Lab")` and that pair is the join key. Nothing is matched on
a similarity score — the rule is a regex over a code the registrar issued.

**A class with no Canvas shell still appears.** LSB 191 meets in ARM L1-17 every Monday
and has no Canvas course at all. A page that listed Canvas courses would silently drop a
class the owner attends, so the calendar is what decides that a course exists and Canvas
only decorates it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime

from backglass.config import Settings
from backglass.coursework import AssignmentRow
from backglass.coursework import rows as assignment_rows
from backglass.ledger import USER_ID
from backglass.plan import timezones

#: The calendar the syllabus-derived exams and deadlines were written to. Named here
#: because it is a join key, not a preference: rows from Family/Work/asu.edu are the
#: class meetings themselves and are read as meetings, not as dates.
SYLLABUS_CALENDAR = "ASU Fall 2026"

#: `CHM113`, `CHM 113`, `2026FallC-T-CHM113-60105` — subject letters then three digits,
#: which is what every ASU course code is made of. Anchored to a word boundary so the
#: `2026` in the term prefix cannot be read as a course number.
_CODE = re.compile(r"\b([A-Z]{2,4})\s?-?\s?(\d{3})\b")

#: What the shell is: the lecture, or one of the sections that hang off it. Canvas spells
#: it in the code (`-Lab-Fall-2026`, `-LABORATORY`, `-Recitation`), the calendar spells it
#: in parentheses (`CHM 113 (Lab)`), and the archive spells it in the folder name.
_COMPONENTS = (
    ("Lab", re.compile(r"\blab(oratory)?\b", re.I)),
    ("Recitation", re.compile(r"\brecitation\b", re.I)),
)

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _subject(text: str) -> tuple[str, str] | None:
    """`("CHM 113", "Lab")` from any of the three spellings, or None if there is no
    course code in the text at all — an org shell like `TRN-ASUReady-UG` has none, and
    is deliberately not a class."""
    found = _CODE.search(text.upper())
    if not found:
        return None
    subject = f"{found.group(1)} {found.group(2)}"
    for name, pattern in _COMPONENTS:
        if pattern.search(text):
            return subject, name
    return subject, "Lecture"


#: The same regex, published. A second page needs the course out of a Canvas code or a
#: commitment sentence, and a second copy of `_CODE` is how two surfaces come to disagree
#: about which class a piece of work belongs to.
subject_of = _subject


def _in_course_folder(path: str, subject: str) -> bool:
    """Does this drop-folder path sit under this course's directory?

    The archive names a folder for the course and, where a course has more than one
    component, suffixes it: `HON171`, but `CHM113-Lecture` and `CHM113-Lab`. Matching the
    first path segment against both shapes is what keeps a single-component course from
    silently having no documents — which is exactly what a bare `CHM113-` prefix did to
    HON 171 and PSY 101 the first time.

    A `files` external id is the relative path as itself (connectors/files keeps a
    readable id on purpose), so this is a path comparison and not a guess.
    """
    head = path.split("/")[0].upper()
    compressed = subject.replace(" ", "").upper()
    return head == compressed or head.startswith(compressed + "-")


@dataclass(frozen=True)
class Meeting:
    """One component's weekly pattern, as the calendar actually recorded it."""

    component: str
    days: str  # Mon/Wed/Fri
    start_label: str
    end_label: str
    room: str
    instructors: list[str]
    occurrences: int
    next_at: datetime | None

    @property
    def when(self) -> str:
        if not self.days:
            return f"{self.start_label}–{self.end_label}"
        return f"{self.days} {self.start_label}–{self.end_label}"


@dataclass(frozen=True)
class KeyDate:
    """An exam or a dated deadline — one row of the syllabus calendar, as ingested."""

    title: str
    starts_at: datetime
    room: str
    source_item_id: int

    @property
    def day_label(self) -> str:
        return self.starts_at.strftime("%a %-d %b")

    @property
    def time_label(self) -> str:
        return self.starts_at.strftime("%-I:%M %p").lower()


@dataclass(frozen=True)
class Obligation:
    """An open commitment the ledger holds for this course, with its evidence."""

    id: int
    what: str
    due_at: str | None
    estimated_minutes: int | None
    source_item_id: int


@dataclass(frozen=True)
class Document:
    """A file from the course archive, once the drop folder is ingesting it."""

    title: str
    path: str
    source_item_id: int


@dataclass(frozen=True)
class Course:
    subject: str
    meetings: list[Meeting] = field(default_factory=list)
    upcoming: list[AssignmentRow] = field(default_factory=list)
    dates: list[KeyDate] = field(default_factory=list)
    obligations: list[Obligation] = field(default_factory=list)
    documents: list[Document] = field(default_factory=list)
    assignment_total: int = 0

    @property
    def instructors(self) -> list[str]:
        """Every named instructor across the components, in meeting order, once each."""
        seen: list[str] = []
        for meeting in self.meetings:
            for name in meeting.instructors:
                if name not in seen:
                    seen.append(name)
        return seen

    @property
    def rooms(self) -> list[str]:
        seen: list[str] = []
        for meeting in self.meetings:
            if meeting.room and meeting.room not in seen:
                seen.append(meeting.room)
        return seen

    @property
    def next_meeting(self) -> Meeting | None:
        dated = [m for m in self.meetings if m.next_at is not None]
        if not dated:
            return None
        return min(dated, key=lambda m: m.next_at or datetime.max)

    @property
    def next_date(self) -> KeyDate | None:
        return self.dates[0] if self.dates else None


def _payload(raw: object) -> dict[str, object]:
    try:
        loaded = json.loads(str(raw or "{}"))
    except (ValueError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _aware(value: str, tz: str) -> datetime:
    """An ISO instant in the owner's active zone. The calendar connectors write both
    `…Z` and `…-07:00`; a naive value is read as already-local rather than dropped."""
    text = value.replace("Z", "+00:00")
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezones._zone(tz))


def _meetings(
    conn: sqlite3.Connection, tz: str, now: datetime
) -> dict[str, list[Meeting]]:
    """Group every non-retracted class meeting by subject.

    A pattern rather than a list: 43 CHM 113 rows are one line — "Mon/Wed/Fri 12:20 pm –
    1:10 pm, Tempe LSA 191, Wei Wang" — and the count is kept so the line can be checked
    against the ledger rather than believed.
    """
    rows = conn.execute(
        "SELECT si.id, si.title, si.raw_json FROM source_item si "
        "WHERE si.user_id = ? AND si.source = 'calendar:asu' "
        "  AND NOT EXISTS (SELECT 1 FROM source_item_retraction r "
        "                  WHERE r.source_item_id = si.id)",
        (USER_ID, ),
    ).fetchall()

    # component key -> the raw material a Meeting is folded out of
    buckets: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        title = str(row["title"] or "")
        payload = _payload(row["raw_json"])
        named = str(payload.get("course") or title)
        parsed = _subject(named)
        if parsed is None:
            continue
        subject, component = parsed
        try:
            starts = _aware(str(payload["starts_at"]), tz)
            ends = _aware(str(payload["ends_at"]), tz)
        except (KeyError, ValueError):
            continue
        bucket = buckets.setdefault(
            (subject, component),
            {
                "days": Counter(),
                "times": Counter(),
                "rooms": Counter(),
                "instructors": [],
                "count": 0,
                "next": None,
            },
        )
        days = bucket["days"]
        times = bucket["times"]
        rooms = bucket["rooms"]
        assert isinstance(days, Counter) and isinstance(times, Counter)
        assert isinstance(rooms, Counter)
        days[starts.weekday()] += 1
        times[(starts.strftime("%-I:%M %p").lower(), ends.strftime("%-I:%M %p").lower())] += 1
        if payload.get("location"):
            rooms[str(payload["location"])] += 1
        for name in payload.get("instructors") or []:
            listed = bucket["instructors"]
            assert isinstance(listed, list)
            if str(name) not in listed:
                listed.append(str(name))
        bucket["count"] = int(bucket["count"] or 0) + 1
        if starts >= now:
            current = bucket["next"]
            if current is None or starts < current:  # type: ignore[operator]
                bucket["next"] = starts

    out: dict[str, list[Meeting]] = {}
    for (subject, component), bucket in buckets.items():
        days = bucket["days"]
        times = bucket["times"]
        rooms = bucket["rooms"]
        assert isinstance(days, Counter) and isinstance(times, Counter)
        assert isinstance(rooms, Counter)
        # The recurring slot, not every stray instant: a make-up session or a moved
        # final should not rewrite the weekly pattern, so the most common start/end
        # wins and the outliers stay visible as the occurrence count.
        (start_label, end_label), _ = times.most_common(1)[0]
        pattern = "/".join(_WEEKDAYS[d] for d, _ in sorted(days.items()))
        out.setdefault(subject, []).append(
            Meeting(
                component=component,
                days=pattern,
                start_label=start_label,
                end_label=end_label,
                room=rooms.most_common(1)[0][0] if rooms else "",
                instructors=list(bucket["instructors"]),  # type: ignore[arg-type]
                occurrences=int(bucket["count"] or 0),
                next_at=bucket["next"],  # type: ignore[arg-type]
            )
        )
    for meetings in out.values():
        meetings.sort(key=lambda m: (m.component != "Lecture", m.component))
    return out


def _dates(
    conn: sqlite3.Connection, tz: str, today: date
) -> dict[str, list[KeyDate]]:
    """The syllabus-derived calendar, grouped by subject.

    These rows exist because the syllabi were read and their dates written to a calendar
    of their own; `apple_calendar` then ingests that calendar like any other. So the page
    reads them the same way it reads a class meeting — out of the ledger, with the item
    id behind every line, which is CLAUDE.md rule 1 applied to a date.

    Only timed events arrive: the connector drops all-day rows before persistence, so the
    drop deadline and the no-class days are on the owner's calendar and not here. That is
    the connector's call about capacity, not a gap this reader should paper over by
    reading the calendar itself.
    """
    rows = conn.execute(
        "SELECT si.id, si.title, si.raw_json FROM source_item si "
        "WHERE si.user_id = ? AND si.source LIKE 'calendar%' "
        "  AND NOT EXISTS (SELECT 1 FROM source_item_retraction r "
        "                  WHERE r.source_item_id = si.id)",
        (USER_ID, ),
    ).fetchall()

    out: dict[str, list[KeyDate]] = {}
    for row in rows:
        payload = _payload(row["raw_json"])
        if str(payload.get("calendar") or "") != SYLLABUS_CALENDAR:
            continue
        title = str(row["title"] or "")
        parsed = _subject(title)
        subject = parsed[0] if parsed else "Semester"
        try:
            starts = _aware(str(payload["starts_at"]), tz)
        except (KeyError, ValueError):
            continue
        if starts.date() < today:
            continue
        out.setdefault(subject, []).append(
            KeyDate(
                title=title,
                starts_at=starts,
                room=str(payload.get("location") or ""),
                source_item_id=int(row["id"]),
            )
        )
    for dates in out.values():
        dates.sort(key=lambda d: d.starts_at)
    return out


def _obligations(conn: sqlite3.Connection) -> dict[str, list[Obligation]]:
    """Open commitments, grouped by the course they belong to.

    Two ways a commitment reaches a course, and neither is a similarity score. Either the
    item it was extracted from is a file in that course's archive folder — the strong
    join, since the id of a `files` item is its own relative path — or the commitment
    text names the course code, which is what a syllabus sentence like "CHM 113 Exam 1"
    does. Anything that matches neither belongs to no course and is left off this page
    rather than guessed onto one.
    """
    rows = conn.execute(
        "SELECT c.id, c.what, c.due_at, c.estimated_minutes, c.source_item_id, "
        "       si.external_id, si.source "
        "FROM commitment c JOIN source_item si ON si.id = c.source_item_id "
        "WHERE c.user_id = ? AND c.status = 'open' "
        "ORDER BY c.due_at IS NULL, c.due_at",
        (USER_ID, ),
    ).fetchall()

    out: dict[str, list[Obligation]] = {}
    for row in rows:
        subject: str | None = None
        if str(row["source"] or "") == "files":
            path = str(row["external_id"] or "")
            parsed = _subject(path.split("/")[0])
            if parsed is not None and _in_course_folder(path, parsed[0]):
                subject = parsed[0]
        if subject is None:
            named = _subject(str(row["what"] or ""))
            subject = named[0] if named is not None else None
        if subject is None:
            continue
        out.setdefault(subject, []).append(
            Obligation(
                id=int(row["id"]),
                what=str(row["what"]),
                due_at=str(row["due_at"]) if row["due_at"] else None,
                estimated_minutes=row["estimated_minutes"],
                source_item_id=int(row["source_item_id"]),
            )
        )
    return out


def _documents(conn: sqlite3.Connection) -> dict[str, list[Document]]:
    """Archive files, if the drop folder is pointed at the course archive.

    Empty until `INBOX_FOLDER_PATH` is set, and empty is the honest answer then: the
    page says "no documents ingested" rather than naming files it cannot prove are
    there.
    """
    rows = conn.execute(
        "SELECT id, title, external_id FROM source_item "
        "WHERE user_id = ? AND source = 'files' ORDER BY external_id",
        (USER_ID, ),
    ).fetchall()
    out: dict[str, list[Document]] = {}
    for row in rows:
        path = str(row["external_id"] or "")
        parsed = _subject(path.split("/")[0])
        if parsed is None:
            continue
        subject, _component = parsed
        if not _in_course_folder(path, subject):
            continue
        out.setdefault(subject, []).append(
            Document(
                title=str(row["title"] or path),
                path=path,
                source_item_id=int(row["id"]),
            )
        )
    return out


def load(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    now: datetime | None = None,
    upcoming_limit: int = 5,
) -> list[Course]:
    """Every course the semester contains, soonest-meeting first.

    Ordering is by the next meeting rather than alphabetically, because the question the
    page answers is "what is next", and a class that already met today should not sit
    above one that meets in an hour.
    """
    tz = timezones.active_tz(settings, timezones.today_for(settings, now))
    moment = now or timezones.local_now(settings)
    today = moment.date()

    meetings = _meetings(conn, tz, moment)
    dates = _dates(conn, tz, today)
    obligations = _obligations(conn)
    documents = _documents(conn)

    coursework: dict[str, list[AssignmentRow]] = {}
    for row in assignment_rows(conn, limit=1000):
        parsed = _subject(row.course)
        if parsed is None:
            continue
        coursework.setdefault(parsed[0], []).append(row)

    # A course exists because the registrar scheduled it or Canvas enrolled it — never
    # because a sentence contained something shaped like a course code. "Meet me in Hall
    # 502" and "Rm 151" both match the code regex, and both appeared as courses until
    # this line: commitments and documents may only attach to a subject that meetings or
    # Canvas coursework already vouch for.
    real = set(meetings) | set(coursework)
    subjects = real | (set(documents) & real) | (set(obligations) & real)
    subjects.discard("Semester")

    out: list[Course] = []
    for subject in subjects:
        work = coursework.get(subject, [])
        ahead = [
            row
            for row in work
            if row.due_at and str(row.due_at)[:10] >= today.isoformat()
        ]
        out.append(
            Course(
                subject=subject,
                meetings=meetings.get(subject, []),
                upcoming=ahead[:upcoming_limit],
                dates=dates.get(subject, []),
                obligations=obligations.get(subject, []),
                documents=documents.get(subject, []),
                assignment_total=len(work),
            )
        )

    def sort_key(course: Course) -> tuple[int, datetime | str, str]:
        nxt = course.next_meeting
        if nxt is not None and nxt.next_at is not None:
            return (0, nxt.next_at, course.subject)
        return (1, "", course.subject)

    out.sort(key=sort_key)
    return out


def semester_dates(conn: sqlite3.Connection, settings: Settings) -> list[KeyDate]:
    """The dates that belong to no single course — drop deadline, breaks, no-class days."""
    tz = timezones.active_tz(settings, timezones.today_for(settings))
    today = timezones.today_for(settings)
    return _dates(conn, tz, today).get("Semester", [])
