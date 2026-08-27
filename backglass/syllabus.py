"""The reading a class expects you to have done before you turn up.

Owner's ask, 2026-08-27: "we need it to be even more rich, for example reading for human
event ... havent been schedul[ed]."

The Human Event is HON 171, and it is a reading seminar: every Tuesday and Thursday has a
text due before the meeting. None of that was anywhere in the ledger. The syllabus *was*
ingested — `source_item` 10397 — and `extract-commitments@10` read it and produced four
commitments: two essays, "post weekly reading response on Canvas" with no date, and "bring
the Norton Anthology to class". The twenty-eight dated readings in its Course Schedule
became nothing. Canvas is no help either: its side of the course is a 150-word discussion
post per week whose description says "the assigned reading" without ever naming or timing
it.

So the most demanding recurring work in the owner's semester — read Gilgamesh by 9/1, the
Bhagavad-Gita by 9/8, the Aeneid by 10/6, Beowulf by 10/20, Dante by 11/10 — was invisible
to the planner, and every day it planned was built on a day that did not include it.

**Parsed, not extracted.** A course schedule is a table that lost its lines in a PDF —
`WEEK 6  Tu 9/22:  Sophocles, Oedipus Tyrannos` — and the two-tier extraction exists for
prose. A parser is testable against the real syllabus, costs nothing, spends no cap, has
no confidence to threshold, and cannot invent a reading that was never assigned. The model
already had its turn at this document and returned four rows out of thirty.

The trade is that this understands one shape of schedule. That is stated rather than
hidden: `readings` returns what it found, `skipped` returns the lines it deliberately
ignored, and a syllabus it cannot read returns nothing at all rather than a guess.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date

from backglass.config import Settings
from backglass.ledger import USER_ID

#: Where the table starts. Everything before it is policy prose that also contains dates.
_SCHEDULE_HEADING = re.compile(r"course\s+schedule", re.I)

#: `Tu 9/22:` — the day marker that opens one class meeting. The weekday abbreviation is
#: matched but not trusted: it is the month and day that place the meeting, and a
#: syllabus that says "Tu" over a date that is a Wednesday is telling you about its own
#: typo, not about the owner's week.
_MEETING = re.compile(
    r"\b(?:M|Tu|Tue|W|Th|Thu|F|Sa|Su|S)\s+(\d{1,2})\s*/\s*(\d{1,2})\s*:\s*",
    re.I,
)

#: `WEEK 12` — a heading, not a meeting. Cut out so it cannot end up inside the title of
#: the reading that follows it.
_WEEK = re.compile(r"\bWEEK\s+\d+\b", re.I)

#: Meetings that are not reading. Every one of these appears in the owner's own syllabus,
#: and each is something other than a text to have read: a class spent workshopping, a
#: day the university is shut, a deadline `extract-commitments` already found (commitments
#: 504 and 505 — creating them again here would be the same promise twice).
_NOT_A_READING = re.compile(
    r"\b(workshop|break|holiday|no\s+class|reflections?|exam|midterm|final|"
    r"due|presentation|peer\s+review|syllabus\s+overview)\b",
    re.I,
)

#: Where the table stops. Nothing marks the end of a course schedule, so the last meeting
#: ran to the end of the document and took the whole policies section with it as its
#: title — "Final Paper due Course Policies and Guidelines Technical support . This course
#: uses Canvas…". Cut at whichever of these headings comes first.
_AFTER_SCHEDULE = re.compile(
    r"\b(course\s+policies|policies\s+and\s+guidelines|grading|assessment|"
    r"academic\s+integrity|attendance\s+policy|required\s+texts)\b",
    re.I,
)

#: A reading is a title, not a paragraph. Anything longer than this is the parser having
#: run past the end of the table, and truncating says so in the one place a reader looks.
_MAX_TITLE = 160

#: Trailing citation, stripped from the title and kept as evidence. "(Vol. A, pp. 885)" is
#: where the reading *starts*, not how long it is — see `default_reading_minutes`.
_CITATION = re.compile(r"\s*\((?:Vol\.?[^)]*|Canvas)\)\s*$", re.I)


@dataclass(frozen=True)
class Reading:
    """One text, due at the meeting it is discussed in."""

    due: date
    title: str
    #: The line as the syllabus wrote it, kept for rule 1: a claim on the board links to
    #: the sentence behind it, and here the sentence is a row of a table.
    quote: str


def readings(
    text: str, *, year: int, on_or_after: date | None = None
) -> tuple[list[Reading], list[str]]:
    """The Course Schedule as dated readings, and the lines deliberately skipped.

    Both halves are returned because the skips are the part worth reading before trusting
    this: a parser that silently dropped half a semester and a parser that correctly
    ignored six workshops look identical from the outside.

    `on_or_after` drops meetings already past. A reading for a class three weeks ago is
    not an obligation, it is history, and putting it on the board as overdue would hand
    the planner a pile of work nobody can do anything about.
    """
    body = _schedule_body(text)
    if not body:
        return [], []

    found: list[Reading] = []
    skipped: list[str] = []
    for month, day, chunk in _meetings(body):
        try:
            due = date(year, month, day)
        except ValueError:
            skipped.append(f"{month}/{day}: not a date")
            continue
        if on_or_after is not None and due < on_or_after:
            continue
        title = _title(chunk)
        if not title:
            continue
        if _NOT_A_READING.search(title):
            skipped.append(f"{due.isoformat()}: {title}")
            continue
        found.append(Reading(due=due, title=title, quote=chunk.strip()))
    return found, skipped


def _schedule_body(text: str) -> str:
    """Everything after the Course Schedule heading, whitespace normalised.

    PDF text arrives double-spaced and line-broken mid-phrase, so every pattern here
    would otherwise need to tolerate arbitrary whitespace inside every token. Normalising
    once is the difference between one readable regex and six unreadable ones.
    """
    match = _SCHEDULE_HEADING.search(text)
    if match is None:
        return ""
    body = re.sub(r"\s+", " ", text[match.end():])
    end = _AFTER_SCHEDULE.search(body)
    return body[: end.start()] if end else body


def _meetings(body: str) -> list[tuple[int, int, str]]:
    """(month, day, what was listed for it), in the order the syllabus lists them.

    A meeting runs until the next meeting begins, which is what lets a single entry carry
    three creation myths across three quoted titles without them being split into three
    meetings on one date.
    """
    marks = list(_MEETING.finditer(body))
    out: list[tuple[int, int, str]] = []
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(body)
        chunk = body[mark.end():end]
        # The next week's heading belongs to the next week, not to the tail of this
        # meeting's title — without this, "Beowulf" is recorded as "Beowulf WEEK 11".
        chunk = _WEEK.split(chunk)[0]
        out.append((int(mark.group(1)), int(mark.group(2)), chunk))
    return out


def _title(chunk: str) -> str:
    title = _CITATION.sub("", chunk.strip()).strip(" .;,")
    # The curly quotes the anthology uses around every myth title; kept as text, dropped
    # as delimiters, so "“Creation Myth (Iroquois)”" reads as a title and not as a quote
    # of one.
    title = title.replace("“", '"').replace("”", '"')
    title = re.sub(r"\s+", " ", title)
    # Truncated rather than dropped: a title this long means the parser ran past the end
    # of the table, and a reader seeing an ellipsis knows to go and look. Silently
    # keeping 4,000 characters would put the syllabus's entire policies section on the
    # board as the name of an obligation.
    return title if len(title) <= _MAX_TITLE else title[:_MAX_TITLE].rstrip() + "…"


def promote(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    source_item_id: int,
    course: str,
    found: list[Reading],
    dry_run: bool = False,
) -> tuple[int, list[str]]:
    """Write the readings the ledger does not already have.

    Idempotent by the rule that matters here (CLAUDE.md rule 3): identity is the course,
    the date and the text, so a second run over the same syllabus writes nothing. There is
    no external id to key on — a syllabus is one document listing thirty obligations, not
    thirty documents — so the key is what the obligation *is*.
    """
    from backglass.ledger import Ledger

    existing = {
        _key(str(row["what"]), str(row["due_at"] or "")[:10])
        for row in conn.execute(
            "SELECT what, due_at FROM commitment WHERE user_id = ? AND status != 'dropped'",
            (USER_ID,),
        )
    }
    ledger = Ledger(conn, settings)
    written = 0
    notes: list[str] = []
    for reading in found:
        what = f"Read for {course}: {reading.title}"
        if _key(what, reading.due.isoformat()) in existing:
            continue
        written += 1
        notes.append(f"{reading.due.isoformat()}  {reading.title[:60]}")
        if dry_run:
            continue
        ledger.insert_commitment(
            direction="i_owe",
            entity_id=None,
            what=what,
            due_at=reading.due.isoformat(),
            estimated_minutes=settings.reading_minutes,
            # `analyzed` is what makes the planner treat this as homework and split it
            # across sittings — which is exactly right for a hundred pages of Beowulf,
            # and exactly why the estimate is a flat knob rather than invented page maths.
            estimate_source="analyzed",
            confidence=1.0,
            source_item_id=source_item_id,
            # Rule 1: the row of the table this came from, verbatim.
            evidence=reading.quote,
            evidence_kind="original",
        )
        existing.add(_key(what, reading.due.isoformat()))
    return written, notes


def _key(what: str, due: str) -> tuple[str, str]:
    return (re.sub(r"[^a-z0-9]+", " ", what.lower()).strip(), due)


#: A syllabus, as the drop folder names one. Matched on the item's title rather than its
#: text, because "syllabus" appears in the body of every course document ever written.
_IS_SYLLABUS = re.compile(r"\bsyllab(us|i)\b", re.I)

#: `HON 171`, `CHM113` — the course this document belongs to, taken from its filename.
#: The body would be a better source and is a worse one in practice: every syllabus names
#: three other courses in its prerequisites.
_COURSE_CODE = re.compile(r"\b([A-Z]{2,4})\s*(\d{3})\b")


def promote_from_ledger(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    on_or_after: date | None = None,
    dry_run: bool = False,
) -> tuple[int, list[str]]:
    """Every syllabus in the ledger, read for the work it schedules.

    Reads `source_item.body_text` rather than the file on disk. The stored item is what
    rule 1 will cite, `/source/{id}` already renders it, and a parser that read the drop
    folder instead would produce claims whose evidence lives somewhere the dashboard
    cannot show — true today, and unverifiable the day the file is moved.
    """
    rows = conn.execute(
        # The title filter is in SQL and not only in Python, because this runs on every
        # sync — 48 times a day, forever — and the ledger holds 11,000 items of which
        # 5,491 are mail carrying up to 20KB of body each. Fetching all of that across
        # the process boundary to throw nearly all of it away on a regex would make a
        # cheap deterministic pass the most expensive thing in the run. `LIKE` is the
        # coarse gate; `_IS_SYLLABUS` below is still the one that decides.
        "SELECT id, title, body_text FROM source_item "
        "WHERE user_id = ? AND body_text IS NOT NULL AND body_text != '' "
        "  AND lower(title) LIKE '%syllab%' "
        "  AND NOT EXISTS (SELECT 1 FROM source_item_retraction r "
        "                  WHERE r.source_item_id = source_item.id) "
        "ORDER BY id",
        (USER_ID,),
    ).fetchall()

    written = 0
    notes: list[str] = []
    for row in rows:
        title = str(row["title"] or "")
        if not _IS_SYLLABUS.search(title):
            continue
        course = _course_of(title)
        if not course:
            notes.append(f"skipped {title[:50]}: no course code in the filename")
            continue
        found, skipped = readings(
            str(row["body_text"]),
            year=_year_of(title, on_or_after),
            on_or_after=on_or_after,
        )
        if not found:
            continue
        count, wrote = promote(
            conn, settings,
            source_item_id=int(row["id"]), course=course, found=found, dry_run=dry_run,
        )
        written += count
        if count:
            notes.append(f"{course}: {count} reading(s) from {title[:40]}")
            notes.extend(f"  {line}" for line in wrote)
        if skipped:
            # Said out loud. A parser that dropped half a semester and one that correctly
            # ignored six workshops look identical from the outside, and this is the only
            # place the difference is visible.
            notes.append(f"  ({len(skipped)} meeting(s) skipped as not reading)")
    return written, notes


def _course_of(title: str) -> str:
    match = _COURSE_CODE.search(title.upper())
    return f"{match.group(1)} {match.group(2)}" if match else ""


def _year_of(title: str, fallback: date | None) -> int:
    """The academic year this schedule belongs to.

    From the filename when it says so — "HON 171 Syllabus - Fall 2026.pdf" — because a
    schedule written in month/day carries no year at all, and guessing from the run date
    puts a January class in the wrong one every spring.
    """
    match = re.search(r"\b(20\d{2})\b", title)
    if match:
        return int(match.group(1))
    return (fallback or date.today()).year

