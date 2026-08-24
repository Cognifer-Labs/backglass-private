"""How long an assignment takes, and what has to be open in front of you to do it.

The complaint this answers, 2026-08-20: *"be able to look at assignment and dynamically
decide time needed and materials needed."*

What it was before. 159 Canvas assignments were in the ledger and 120 of the open ones
carried the same `type_default:30`, because `plan/estimates.py` classifies the *sentence*
a commitment is made of — "Complete LearningCurve 14a" matches the `form` pattern, so does
"Complete Exam 3", and both come out half an hour. That is not a bad table; it is a table
being asked a question it cannot see the evidence for. The evidence is in the assignment,
and the assignment was being thrown away at the connector.

So this module reads the assignment. Two layers, and only the first one is here:

  1. **What the text states outright.** A runtime in the title (`(12:35)`), a word count, a
     question count, a chapter range. These are not estimates — they are numbers the
     course published, and the row keeps the words it read them from. Where nothing is
     stated, an ASU-shaped type table (`coursework_defaults`) answers instead, which is
     the same fallback shape `estimate_defaults` already uses and is honest about being a
     default rather than a reading.
  2. The model, for what the prose implies and no regex will reach — increment D, not
     here. It writes to the same columns behind the same spend cap as `relevance.py`, and
     it is deliberately not the first thing built: half the feed states its own size, and
     paying a model to re-read a number already printed in the title would be silly.

**Additive, per CLAUDE.md.** The obligation still lives in `commitment`, the evidence
still lives in `source_item`. If this table vanished the planner would go back to a flat
thirty minutes and every surface would still be correct.

**Idempotent, per rule 3.** `description_hash` is what decides that an assignment changed,
not the fetch — a re-read of an unchanged feed writes nothing at all, including
timestamps, which is why the column is `last_changed_at` and not `last_seen_at`.
"""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from dataclasses import dataclass, field

from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID

# ── what the assignment says about itself ─────────────────────────────────────

#: `1-1-1 - Tech in the 21st Century (12:35)`. 38 of the 169 assignments in the owner's
#: feed are videos and most print their runtime exactly like this. Guarded against a time
#: of day — `(2:30 PM)` is when something happens, not how long it lasts.
_RUNTIME = re.compile(r"\((?:(\d{1,2}):)?(\d{1,2}):(\d{2})\)(?!\s*[ap]\.?m)", re.I)

#: "800 words", "800-1000 words". The upper bound is the one that matters: an assignment
#: asking for 800 to 1000 words is finished at 1000, not at 800.
_WORDS = re.compile(r"\b(\d{2,5})(?:\s*(?:-|–|to)\s*(\d{2,5}))?\s*words?\b", re.I)

#: A deliverable measured in pages — "a 3-page memo", "3 to 4 pages". Reading pages are a
#: different unit entirely and are matched separately below, because writing a page and
#: reading a page differ by an order of magnitude.
_PAGES_WRITTEN = re.compile(r"\b(\d{1,2})(?:\s*(?:-|–|to)\s*(\d{1,2}))?[\s-]*pages?\b", re.I)

#: "pp. 45-70", "pages 45-70" — a range with a start well above one is somebody's book,
#: not a length requirement.
_PAGES_READ = re.compile(r"\b(?:pp?\.|pages)\s*(\d{1,3})\s*(?:-|–|to)\s*(\d{1,3})\b", re.I)

#: "20 questions", "15 problems".
_QUESTIONS = re.compile(
    r"\b(\d{1,3})\s*(?:multiple[\s-]choice\s*)?(?:questions|problems|items)\b", re.I
)

#: "Ch. 13-15", "Chapters 4 through 6", "Ch. 13, 14, and 15", "Chapter 9". The run after
#: the word is captured whole and expanded separately, because a range and a list are the
#: same fact written two ways: the first version of this matched a range only, read the
#: owner's "Exam 4 (Ch. 13, 14, and 15)" as chapters 13 to 14, and quietly took a third
#: off the revision it scheduled.
#: The separator repeats, because "13, 14, and 15" puts two of them between the last
#: pair — a comma and the word "and" — and a single-separator alternation stops at 14.
_CHAPTERS = re.compile(
    r"\bch(?:apters?|\.)?\s*"
    r"(\d{1,2}(?:(?:\s*(?:[-–,&]|and|through|to)\s*)+\d{1,2})*)\b",
    re.I,
)
_RANGE_IN_RUN = re.compile(r"(\d{1,2})\s*(?:-|–|through|to)\s*(\d{1,2})")

#: Minutes per unit. Each is a rate applied to a number the assignment stated, so the
#: product is only as arguable as the rate — and the rate is written down here rather than
#: buried in an expression, so it can be argued with.
VIDEO_OVERHEAD_MINUTES = 5  # the questions that follow a Canvas video, and the notes
MINUTES_PER_QUESTION = 2
WORDS_PER_MINUTE = 8  # drafting, not typing: ~480 words an hour including the thinking
WORDS_PER_PAGE = 275
MINUTES_PER_PAGE_READ = 3
MINUTES_PER_CHAPTER = 40

#: Coursework types, most specific first — the first match wins, so the order is the
#: ranking, exactly as in `plan/estimates.py`. The vocabulary is the owner's real feed:
#: LearningCurve and Concept Practice are Macmillan Achieve's adaptive drills, an RFP and
#: a milestone are the CIS 236 project sequence, and "T -" prefixes its team assignments.
TYPE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Ahead of everything, because an administrative form that happens to say the word
    # "exam" in its instructions is not an exam. "Excuse Note Submission Link &
    # Instructions" came out of the first pass as 120 minutes of revision, and "Excused
    # Absence Requests" as a 90-minute lab, both read off a description rather than off
    # what the thing is.
    (
        "form",
        re.compile(
            r"\b(excuse[ds]?|absence request|submission link|permission|waiver|consent|"
            r"sign[- ]?up|attendance|syllabus quiz|acknowledg)\w*\b",
            re.I,
        ),
    ),
    ("learningcurve", re.compile(r"\b(learning ?curve|concept practice|achieve)\b", re.I)),
    # `final` alone is not a signal — course prose is full of "the final milestone", and
    # on the first live pass "T - Team Planning" became a 120-minute exam because its
    # description used the word. An exam says so.
    ("exam", re.compile(r"\b(exam|midterm|final exam)\b", re.I)),
    ("quiz", re.compile(r"\b(quiz|knowledge check)\b", re.I)),
    ("lab", re.compile(r"\b(lab|laboratory|pre-?lab|post-?lab|recitation)\b", re.I)),
    (
        "milestone",
        re.compile(r"\b(rfp|proposal|presentation|milestone|capstone|portfolio|draft)\b", re.I),
    ),
    ("discussion", re.compile(r"\b(discussion|peer review|respond to|reply to)\b", re.I)),
    ("video", re.compile(r"\b(video|watch|recording|lecture capture)\b", re.I)),
    ("reading", re.compile(r"\b(read|reading|textbook|chapter)\b", re.I)),
    ("homework", re.compile(r"\b(hw|homework|problem set|worksheet)\b", re.I)),
    ("module", re.compile(r"\b(module|tutorial|simulation)\b", re.I)),
]

#: Tools an assignment names, and what they are. Every one of these appears in the owner's
#: own feed: LockDown Browser gates five exams, WeVideo carries the CIS 236 lectures,
#: Tableau and Solver are the analytics coursework. The point of naming them is the
#: evening before — "this exam needs a browser you have not installed" is worth more than
#: any estimate on this page.
TOOL_LEXICON: list[tuple[str, str, str]] = [
    (r"lockdown browser", "software", "Respondus LockDown Browser"),
    (r"\bwevideo\b", "software", "WeVideo"),
    (r"\btableau\b", "software", "Tableau"),
    (r"\bsolver\b", "software", "Excel Solver"),
    (r"\bexcel\b", "software", "Microsoft Excel"),
    (r"\bpower ?bi\b", "software", "Power BI"),
    (r"\bspss\b", "software", "SPSS"),
    (r"\br ?studio\b", "software", "RStudio"),
    (r"\bjupyter\b", "software", "Jupyter"),
    (r"\bachieve\b", "software", "Macmillan Achieve"),
    (r"\bzoom\b", "software", "Zoom"),
    (r"\bslack\b", "software", "Slack"),
    (r"\byoutube\b", "software", "YouTube"),
    (r"\bgoggles\b|\blab coat\b", "other", "Lab safety kit"),
    (r"\bcalculator\b", "other", "Calculator"),
]

#: `[WeVideo User Guide] (http://links.asu.edu/WeVideo-Learner)` — how Canvas renders a
#: link inside the plain-text half of the description.
_LINK = re.compile(r"\[([^\]\n]{1,90})\]\s*\((https?://[^)\s]+)\)")


def _chapters_in(text: str) -> tuple[set[int], re.Match[str]] | None:
    """Which chapters a phrase names, and the phrase itself.

    A list and a range are the same fact written two ways, so both expand to the same set
    and the count is what the estimate is built from.
    """
    hit = _CHAPTERS.search(text or "")
    if hit is None:
        return None
    run = hit.group(1)
    numbers = {int(n) for n in re.findall(r"\d{1,2}", run)}
    for span in _RANGE_IN_RUN.finditer(run):
        low, high = sorted((int(span.group(1)), int(span.group(2))))
        numbers.update(range(low, high + 1))
    return (numbers, hit) if numbers else None


def _chapter_name(numbers: set[int]) -> str:
    ordered = sorted(numbers)
    if len(ordered) == 1:
        return f"Ch. {ordered[0]}"
    contiguous = ordered == list(range(ordered[0], ordered[-1] + 1))
    if contiguous:
        return f"Ch. {ordered[0]}–{ordered[-1]}"
    return "Ch. " + ", ".join(str(n) for n in ordered)


@dataclass(frozen=True)
class Effort:
    minutes: int
    basis: str
    quote: str
    sessions: int


@dataclass(frozen=True)
class Material:
    kind: str
    name: str
    detail: str = ""
    quote: str = ""
    basis: str = "deterministic"

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.name)


def defaults(settings: Settings) -> dict[str, int]:
    table: dict[str, int] = {}
    for entry in settings.coursework_defaults:
        name, _, value = entry.partition(":")
        try:
            table[name.strip()] = int(value)
        except ValueError:
            continue
    table.setdefault("assignment", 45)
    return table


def classify(title: str, description: str = "") -> str:
    """What kind of coursework this is. The title is asked first, and alone.

    Not one blob of title-plus-description, which is what it was: a CHM 113 absence form
    whose instructions mention missing an exam came out as an exam and was given two
    hours of revision. A description is context; the title is what the thing *is*, and
    the description only answers when the title says nothing at all.
    """
    for scope in (title, description):
        if not scope:
            continue
        for kind, pattern in TYPE_PATTERNS:
            if pattern.search(scope):
                return kind
    return "assignment"


def _quote_around(text: str, start: int, end: int, *, width: int = 140) -> str:
    """The words a number was read from, bounded.

    Rule 1 inside this table: the row records what it read, so a number that looks wrong
    can be checked against the sentence rather than against this file. Bounded because a
    12,000-character CIS 236 description would otherwise land whole in a column somebody
    reads on a dashboard.
    """
    head = max(0, start - width // 2)
    tail = min(len(text), end + width // 2)
    snippet = " ".join(text[head:tail].split())
    return ("…" if head else "") + snippet + ("…" if tail < len(text) else "")


def _stated_effort(title: str, description: str) -> Effort | None:
    """A number the course itself published, or None.

    Ordered by how directly the number answers "how long". A runtime *is* the duration; a
    word count is a size the rate turns into a duration; a chapter range is the loosest of
    the three and comes last.
    """
    title_first = f"{title}\n{description}"

    runtime = _RUNTIME.search(title_first)
    if runtime:
        hours = int(runtime.group(1) or 0)
        minutes = hours * 60 + int(runtime.group(2)) + math.ceil(int(runtime.group(3)) / 60)
        return Effort(
            minutes=minutes + VIDEO_OVERHEAD_MINUTES,
            basis="stated_runtime",
            quote=_quote_around(title_first, runtime.start(), runtime.end()),
            sessions=1,
        )

    # The largest figure, not the first. A CIS 236 milestone description runs 12,000
    # characters and mentions its own size several times — "Finalize your integrated team
    # RFP (3 pages)" appears above "Maximum 10 pages for the report body" — so first-match
    # -wins read the largest deliverable in the semester as the smallest number in its own
    # instructions. Which mention comes first in a wall of prose is not evidence.
    words = _largest(_WORDS, title_first)
    if words:
        count, hit = words
        return Effort(
            minutes=max(15, round(count / WORDS_PER_MINUTE)),
            basis="stated_words",
            quote=_quote_around(title_first, hit.start(), hit.end()),
            sessions=1,
        )

    read = _PAGES_READ.search(title_first)
    if read:
        pages = max(1, int(read.group(2)) - int(read.group(1)) + 1)
        return Effort(
            minutes=pages * MINUTES_PER_PAGE_READ,
            basis="stated_pages_read",
            quote=_quote_around(title_first, read.start(), read.end()),
            sessions=1,
        )

    written = _largest(_PAGES_WRITTEN, title_first)
    if written:
        pages, hit = written
        return Effort(
            minutes=max(15, round(pages * WORDS_PER_PAGE / WORDS_PER_MINUTE)),
            basis="stated_pages_written",
            quote=_quote_around(title_first, hit.start(), hit.end()),
            sessions=1,
        )

    questions = _QUESTIONS.search(title_first)
    if questions:
        return Effort(
            minutes=max(10, int(questions.group(1)) * MINUTES_PER_QUESTION),
            basis="stated_questions",
            quote=_quote_around(title_first, questions.start(), questions.end()),
            sessions=1,
        )

    chapters = _chapters_in(title_first)
    if chapters:
        numbers, hit = chapters
        return Effort(
            minutes=len(numbers) * MINUTES_PER_CHAPTER,
            basis="stated_chapters",
            quote=_quote_around(title_first, hit.start(), hit.end()),
            sessions=1,
        )
    return None


def _largest(pattern: re.Pattern[str], text: str) -> tuple[int, re.Match[str]] | None:
    """The biggest number this pattern finds anywhere, with the match it came from."""
    best: tuple[int, re.Match[str]] | None = None
    for hit in pattern.finditer(text):
        value = max(int(g) for g in hit.groups() if g)
        if best is None or value > best[0]:
            best = (value, hit)
    return best


def effort_for(title: str, description: str, settings: Settings) -> Effort:
    """Minutes, the basis they came from, and how many sittings that is.

    A stated number beats the type table every time — that is the whole asymmetry of this
    module. "Take PSY101 Exam 4 (Ch. 13-15)" is three chapters of revision, not the thirty
    minutes its verb suggests, and the difference is printed in the title.
    """
    stated = _stated_effort(title, description)
    table = defaults(settings)
    if stated is not None:
        minutes = stated.minutes
        basis, quote = stated.basis, stated.quote
    else:
        kind = classify(title, description)
        minutes = table[kind]
        basis, quote = f"type:{kind}", ""
    return Effort(
        minutes=minutes,
        basis=basis,
        quote=quote,
        sessions=max(1, math.ceil(minutes / max(1, settings.max_block_minutes))),
    )


def materials_for(title: str, description: str) -> list[Material]:
    """What has to be in front of the owner before the work can start.

    Deterministic and therefore incomplete on purpose: every row here is something the
    assignment named in words this module can point at. A material with no quote would be
    a guess the owner has to go and verify by hand, which is the work this is meant to
    remove rather than create.
    """
    text = f"{title}\n{description}"
    out: dict[tuple[str, str], Material] = {}

    for pattern, kind, name in TOOL_LEXICON:
        hit = re.search(pattern, text, re.I)
        if hit is None:
            continue
        material = Material(
            kind=kind, name=name, quote=_quote_around(text, hit.start(), hit.end())
        )
        out.setdefault(material.key, material)

    chapters = _chapters_in(text)
    if chapters:
        numbers, hit = chapters
        material = Material(
            kind="reading",
            name=_chapter_name(numbers),
            detail="course textbook",
            quote=_quote_around(text, hit.start(), hit.end()),
        )
        out.setdefault(material.key, material)

    for link in _LINK.finditer(description):
        label = " ".join(link.group(1).split())
        material = Material(
            kind="link",
            name=label[:90],
            detail=link.group(2),
            quote=_quote_around(description, link.start(), link.end()),
        )
        out.setdefault(material.key, material)

    return sorted(out.values(), key=lambda m: (m.kind, m.name))


# ── the record ────────────────────────────────────────────────────────────────


@dataclass
class CourseworkReport:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    materials_added: int = 0
    materials_removed: int = 0
    estimates_applied: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def writes(self) -> int:
        return (
            self.inserted
            + self.updated
            + self.materials_added
            + self.materials_removed
            + self.estimates_applied
        )


def description_hash(description: str) -> str:
    return hashlib.sha256(description.encode("utf-8")).hexdigest()


def _source_item_id(conn: sqlite3.Connection, source: str, external_id: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM source_item WHERE user_id = ? AND source = ? AND external_id = ?",
        (USER_ID, source, external_id),
    ).fetchone()
    return int(row["id"]) if row else None


def upsert(
    conn: sqlite3.Connection,
    settings: Settings,
    source: str,
    assignments: list,
    *,
    dry_run: bool = False,
) -> CourseworkReport:
    """Write one `assignment` row per feed entry. Rule 3: an unchanged feed writes nothing.

    `assignments` is the connector's own `ParsedAssignment` list, taken off the object
    after its fetch — the same attribute seam `seen_chats` and `excluded_by_rule` use. It
    is typed loosely here so this module does not import a connector: the ledger does not
    depend on where the reading came from, only on what it said.
    """
    report = CourseworkReport()
    stamp = now_iso()

    for parsed in assignments:
        digest = description_hash(parsed.description)
        effort = effort_for(parsed.title, parsed.description, settings)
        materials = materials_for(parsed.title, parsed.description)

        existing = conn.execute(
            "SELECT * FROM assignment WHERE user_id = ? AND source = ? AND external_id = ?",
            (USER_ID, source, parsed.external_id),
        ).fetchone()

        if existing is None:
            report.inserted += 1
            if dry_run:
                # -1 matches no stored row, so every derived material counts as an
                # addition and nothing is written — a dry run that under-reported its
                # own writes would be worse than no dry run.
                _sync_materials(conn, -1, materials, report, stamp, dry_run=True)
                continue
            cursor = conn.execute(
                "INSERT INTO assignment (user_id, source, external_id, source_item_id, "
                " course, title, due_at, url, description, description_hash, "
                " effort_minutes, effort_basis, effort_quote, sessions, analyzed_hash, "
                " first_seen_at, last_changed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    USER_ID,
                    source,
                    parsed.external_id,
                    _source_item_id(conn, source, parsed.external_id),
                    parsed.course,
                    parsed.title,
                    parsed.due_at,
                    parsed.url,
                    parsed.description,
                    digest,
                    effort.minutes,
                    effort.basis,
                    effort.quote,
                    effort.sessions,
                    digest,
                    stamp,
                    stamp,
                ),
            )
            _sync_materials(conn, int(cursor.lastrowid or 0), materials, report, stamp)
            continue

        changed = {
            "course": parsed.course,
            "title": parsed.title,
            "due_at": parsed.due_at,
            "url": parsed.url,
            "description": parsed.description,
            "description_hash": digest,
            "effort_minutes": effort.minutes,
            "effort_basis": effort.basis,
            "effort_quote": effort.quote,
            "sessions": effort.sessions,
            "analyzed_hash": digest,
        }
        # A row the model pass (increment D) has spoken about keeps its number: the
        # deterministic layer is the floor, and re-deriving over the top of a richer
        # reading on every sync would undo it silently once a run.
        if str(existing["effort_basis"] or "").startswith("model"):
            for column in ("effort_minutes", "effort_basis", "effort_quote", "sessions"):
                changed[column] = existing[column]
            changed["analyzed_hash"] = existing["analyzed_hash"]

        # An assignment first recorded by `backglass coursework --refresh` — which reads
        # the feed without ingesting it — has no item to point at yet. The link is made
        # the first time the row is seen after the sync that stored the item, rather than
        # being lost because the insert happened to come first.
        if existing["source_item_id"] is None:
            found = _source_item_id(conn, source, parsed.external_id)
            if found is not None:
                changed["source_item_id"] = found

        diff = {k: v for k, v in changed.items() if existing[k] != v}
        material_changes = _sync_materials(
            conn, int(existing["id"]), materials, report, stamp, dry_run=dry_run
        )
        if not diff:
            if not material_changes:
                report.unchanged += 1
            continue

        report.updated += 1
        if "due_at" in diff and existing["due_at"]:
            report.notes.append(
                f"{parsed.course or source} — {parsed.title}: due date moved "
                f"{str(existing['due_at'])[:10]} → {str(parsed.due_at)[:10]} upstream"
            )
        if dry_run:
            continue
        assignments_sql = ", ".join(f"{column} = ?" for column in diff)
        conn.execute(
            f"UPDATE assignment SET {assignments_sql}, last_changed_at = ? WHERE id = ?",
            (*diff.values(), stamp, int(existing["id"])),
        )

    return report


def _sync_materials(
    conn: sqlite3.Connection,
    assignment_id: int,
    materials: list[Material],
    report: CourseworkReport,
    stamp: str,
    *,
    dry_run: bool = False,
) -> int:
    """Make the stored material set equal the derived one. Returns how many rows moved.

    Insert-missing and delete-extra rather than delete-all-and-reinsert: the second is one
    line shorter and makes every sync a write for every assignment, which is the rule 3
    failure this whole module is careful about.
    """
    existing = {
        (str(row["kind"]), str(row["name"])): row
        for row in conn.execute(
            "SELECT * FROM assignment_material WHERE user_id = ? AND assignment_id = ?",
            (USER_ID, assignment_id),
        )
    }
    derived = {m.key: m for m in materials}
    moved = 0

    for key, material in derived.items():
        if key in existing:
            continue
        moved += 1
        report.materials_added += 1
        if dry_run:
            continue
        conn.execute(
            "INSERT INTO assignment_material (user_id, assignment_id, kind, name, detail, "
            " quote, basis, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                USER_ID,
                assignment_id,
                material.kind,
                material.name,
                material.detail,
                material.quote,
                material.basis,
                stamp,
            ),
        )

    for key, row in existing.items():
        # The model's rows are not this layer's to withdraw. Increment D writes them from
        # evidence a regex cannot see, so "I did not derive it" is not a contradiction.
        if key in derived or str(row["basis"]) != "deterministic":
            continue
        moved += 1
        report.materials_removed += 1
        if dry_run:
            continue
        conn.execute("DELETE FROM assignment_material WHERE id = ?", (int(row["id"]),))

    return moved


def apply_estimates(
    conn: sqlite3.Connection, *, dry_run: bool = False
) -> tuple[int, list[str]]:
    """Push the assignment's effort onto the commitment the planner actually reads.

    The ladder is manual > extracted > analyzed > type_default, and it is a statement
    about evidence rather than recency: a number the owner chose outranks one the source
    text stated, which outranks one derived here, which outranks a table default. So this
    only ever overwrites `type_default`, a NULL, or its own previous answer.
    """
    rows = conn.execute(
        "SELECT a.id, a.title, a.effort_minutes, c.id AS commitment_id, "
        "       c.estimated_minutes, c.estimate_source "
        "FROM assignment a "
        "JOIN commitment c ON c.source_item_id = a.source_item_id "
        "WHERE a.user_id = ? AND a.effort_minutes IS NOT NULL "
        "  AND c.status = 'open' "
        "  AND (c.estimate_source IS NULL "
        "       OR c.estimate_source IN ('type_default', 'analyzed'))",
        (USER_ID,),
    ).fetchall()

    applied = 0
    notes: list[str] = []
    for row in rows:
        same = row["estimated_minutes"] == row["effort_minutes"]
        if same and row["estimate_source"] == "analyzed":
            continue  # rule 3
        applied += 1
        notes.append(
            f"commitment {row['commitment_id']}: {row['estimated_minutes'] or '—'}m → "
            f"{row['effort_minutes']}m ({row['title'][:48]})"
        )
        if dry_run:
            continue
        conn.execute(
            "UPDATE commitment SET estimated_minutes = ?, estimate_source = 'analyzed' "
            "WHERE id = ?",
            (int(row["effort_minutes"]), int(row["commitment_id"])),
        )
    return applied, notes


def apply_due_dates(
    conn: sqlite3.Connection, *, dry_run: bool = False
) -> tuple[int, list[str]]:
    """Carry a moved due date onto the commitment the planner and the brief actually read.

    The half of the Canvas loop that was missing. `source_item` is immutable, the due date
    is in its `body_text` and its `occurred_at`, and both are hashed — so when Canvas moves
    a date the re-read is a `content_hash` difference on a row that cannot be updated. The
    `assignment` table (migration 0031) was built to hold the moving half and `upsert`
    already writes the new date there. Nothing carried it the last step, so on 2026-08-23
    the ledger held both answers at once: `assignment:7833000` due 08-25 in one table and
    commitment 356 due 08-23 in the other, with the planner reading the stale one and
    scheduling work for a day that was no longer the deadline.

    **Only forward from the feed, and only while the obligation is open.** A resolved or
    dropped commitment is history and does not get rewritten. `due_at` on the commitment
    may carry a time (`2026-05-18T13:00:00`) where the feed states a date, so the
    comparison is on the date, and a move writes the feed's value whole — the feed is the
    authority on when this is due, and inventing a time it did not state would be a claim
    with no evidence behind it.

    Every move emits a `claim_event`. That is what makes it findable afterwards: rule 1
    says a generated claim links to its source, and "the deadline you were shown moved"
    is exactly the kind of silent change that costs trust when it cannot be traced. It is
    also the wiring pass migration 0032's own header defers to this change.
    """
    from backglass import claim_events

    rows = conn.execute(
        "SELECT a.id, a.title, a.due_at, c.id AS commitment_id, c.due_at AS commitment_due "
        "FROM assignment a "
        "JOIN commitment c ON c.source_item_id = a.source_item_id "
        "WHERE a.user_id = ? AND a.due_at IS NOT NULL AND c.status = 'open'",
        (USER_ID,),
    ).fetchall()

    applied = 0
    notes: list[str] = []
    for row in rows:
        current = str(row["commitment_due"] or "")
        if current[:10] == str(row["due_at"])[:10]:
            continue  # rule 3: the dates already agree, so there is nothing to write
        applied += 1
        notes.append(
            f"commitment {row['commitment_id']}: due {current[:10] or '—'} → "
            f"{str(row['due_at'])[:10]} ({row['title'][:48]})"
        )
        if dry_run:
            continue
        conn.execute(
            "UPDATE commitment SET due_at = ? WHERE id = ?",
            (row["due_at"], int(row["commitment_id"])),
        )
        claim_events.record(
            conn,
            subject_table="commitment",
            subject_id=int(row["commitment_id"]),
            field="due_at",
            old_value=current or None,
            new_value=str(row["due_at"]),
            cause="canvas:due_moved",
        )
    return applied, notes


@dataclass(frozen=True)
class AssignmentRow:
    id: int
    course: str
    title: str
    due_at: str | None
    effort_minutes: int | None
    effort_basis: str | None
    effort_quote: str | None
    sessions: int
    url: str
    materials: list[Material]


def rows(conn: sqlite3.Connection, *, limit: int = 200) -> list[AssignmentRow]:
    """Every assignment with its effort and its materials, soonest first.

    Ordered by due date with the undated last, because the question this answers is
    "what is coming" and an assignment with no date is not coming on any particular day.
    """
    found = conn.execute(
        "SELECT * FROM assignment WHERE user_id = ? "
        "ORDER BY due_at IS NULL, due_at, id LIMIT ?",
        (USER_ID, limit),
    ).fetchall()
    out: list[AssignmentRow] = []
    for row in found:
        materials = [
            Material(
                kind=str(m["kind"]),
                name=str(m["name"]),
                detail=str(m["detail"] or ""),
                quote=str(m["quote"] or ""),
                basis=str(m["basis"] or "deterministic"),
            )
            for m in conn.execute(
                "SELECT * FROM assignment_material WHERE user_id = ? AND assignment_id = ? "
                "ORDER BY kind, name",
                (USER_ID, int(row["id"])),
            )
        ]
        out.append(
            AssignmentRow(
                id=int(row["id"]),
                course=str(row["course"] or ""),
                title=str(row["title"]),
                due_at=str(row["due_at"]) if row["due_at"] else None,
                effort_minutes=row["effort_minutes"],
                effort_basis=str(row["effort_basis"]) if row["effort_basis"] else None,
                effort_quote=str(row["effort_quote"]) if row["effort_quote"] else None,
                sessions=int(row["sessions"] or 1),
                url=str(row["url"] or ""),
                materials=materials,
            )
        )
    return out
