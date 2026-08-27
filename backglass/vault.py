"""The vault Backglass writes: the ledger, rendered as an Obsidian vault.

Asked for on 2026-08-23. The owner already had four vaults on disk and wanted none of
them ingested — the request was for a vault *about them*, built out of what Backglass
knows, "along with an evolving state document that holds everything major to check
against."

So this is the mirror of `connectors/notes.py`. That module reads a vault into the
ledger; this one writes the ledger into a vault. Together they make one folder that the
owner opens in Obsidian, reads their own record in, and writes new notes into.

Three properties make that safe to point at a live vault:

- **Generated files are marked and never re-ingested.** Every note here carries
  `backglass: generated` in its frontmatter and `notes.py` drops it on sight. Without
  that, setting `OBSIDIAN_VAULT_PATH` to this folder would have the next sync ingest
  Backglass's own output as immutable-forever `source_item` rows and extract facts from
  its own facts. The marker is in the file rather than in a skipped path because a path
  rule dies the first time the owner drags a note somewhere else, and Obsidian is a tool
  for dragging notes somewhere else.
- **Unchanged notes are not rewritten.** Bytes are compared before writing, so a second
  export writes nothing (rule 3), and — because a rewrite would bump the mtime — a note
  that did not change does not cost the next sync a re-read either. Nothing here carries
  a timestamp except `STATE.md`, which is a snapshot and says so.
- **Every claim keeps its provenance.** A row with a `source_item_id` links to the
  dashboard's `/source/<id>` (rule 1); a row without one says "no source" rather than
  implying it had one. Low-confidence commitments are rendered under their own heading,
  named as unconfirmed, never mixed into the list the owner reads as settled (rule 2).

The vault is a report, not a store. Delete it and run the export again and it comes back
identical; nothing in the ledger depends on a file here existing.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from backglass.config import Settings
from backglass.db import query
from backglass.ledger import USER_ID

#: The frontmatter key `connectors/notes.py` reads to know a file is ours. Changing it
#: is a two-file change; the connector's test names this constant for that reason.
MARK_KEY = "backglass"
MARK_VALUE = "generated"

#: Characters Obsidian and the filesystem disagree about. A note called "CHM 113 / lab"
#: is one file with a slash in the name on neither.
_UNSAFE = '\\/:*?"<>|#^[]'

#: The folders the export writes into, and therefore the only ones it will delete from.
#:
#: The marker says a file was generated; it does not say the owner still wants it where it
#: is. Dragging `Facts/housing.md` into an `Archive/` folder is a deliberate act, and a
#: prune that reached outside these directories would undo it on the next sync. So the
#: rule is narrower than the marker: the export removes what it no longer produces, in
#: the places it produces things.
OWNED_DIRS = ("Facts", "People", "Classes")


@dataclass(frozen=True)
class Note:
    """One rendered file: a vault-relative posix path and its complete text."""

    path: str
    body: str


@dataclass
class Report:
    root: Path
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    #: Generated notes the ledger no longer produces, deleted. A person whose last open
    #: commitment closed stops being rendered, and a file nobody rewrites is a claim
    #: nobody can source — rule 1 does not stop applying because the writer walked away.
    removed: list[str] = field(default_factory=list)
    #: Notes that could not be written, with the reason. A full disk or a read-only vault
    #: is a degradation, not a failure of the sync that called this (rule 5).
    failed: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.written) + len(self.unchanged) + len(self.failed)

    def line(self) -> str:
        text = (
            f"vault {self.root}: {len(self.written)} written, "
            f"{len(self.unchanged)} unchanged"
        )
        if self.removed:
            text += f", {len(self.removed)} removed"
        if self.failed:
            text += f", {len(self.failed)} failed"
        return text


# ── rendering helpers ────────────────────────────────────────────────────────


def safe_name(name: str) -> str:
    """A filename Obsidian will open and the filesystem will keep."""
    cleaned = "".join(" " if ch in _UNSAFE else ch for ch in name).strip()
    cleaned = " ".join(cleaned.split())
    return cleaned[:80] or "untitled"


def _front(kind: str, extra: dict[str, str] | None = None) -> str:
    lines = ["---", f"{MARK_KEY}: {MARK_VALUE}", f"type: {kind}"]
    for key, value in sorted((extra or {}).items()):
        lines.append(f"{key}: {value}")
    lines += ["---", ""]
    return "\n".join(lines)


def _source(settings: Settings, source_item_id: Any) -> str:
    """Rule 1, in one link. A claim with no source says so instead of implying one."""
    if source_item_id in (None, ""):
        return "_no source_"
    base = settings.dashboard_base_url.rstrip("/")
    return f"[source #{source_item_id}]({base}/source/{source_item_id})"


def _link(path: str, label: str | None = None) -> str:
    stem = path[:-3] if path.endswith(".md") else path
    return f"[[{stem}|{label}]]" if label else f"[[{stem}]]"


def _day(value: Any) -> str:
    return str(value)[:10] if value else "no date"


def _bullet(text: str) -> str:
    return f"- {text}"


#: A course code as the calendar and Canvas both write it — two to four letters, a space,
#: three digits. Narrow on purpose: this is what gets linked inside a sentence, and a
#: looser pattern turns ordinary prose into a field of broken links.
_COURSE_CODE = re.compile(r"\b[A-Z]{2,4} ?\d{3}\b")

#: A wikilink already in the text, so a second pass does not nest one inside another.
_WIKILINK = re.compile(r"\[\[[^\]]*\]\]")


@dataclass(frozen=True)
class Index:
    """Which notes exist, so a mention can become a link to a file that is really there.

    Obsidian will happily render `[[Classes/CHM 113]]` as a link to nothing, and a vault
    of confident links to absent notes is worse than a vault of plain text — the graph
    fills with phantoms and the owner cannot tell a real connection from a typo. So every
    link this module writes is checked against this index first.
    """

    #: Person note path by canonical name, and by lowercased name for matching.
    people: dict[str, str] = field(default_factory=dict)
    #: Class note path by course subject, e.g. "CHM 113".
    classes: dict[str, str] = field(default_factory=dict)
    #: Who teaches each subject, so a person note can say what they teach without
    #: re-loading the semester. The link runs both ways or Obsidian's graph only has half
    #: the edge — a class naming its instructor and an instructor naming nothing.
    taught_by: dict[str, list[str]] = field(default_factory=dict)

    def person(self, name: str | None) -> str | None:
        if not name:
            return None
        return self.people.get(name) or self.people.get(name.strip().lower())

    def course(self, subject: str | None) -> str | None:
        if not subject:
            return None
        return self.classes.get(subject) or self.classes.get(subject.strip().upper())


def _mentions(text: str, index: Index) -> str:
    """Link the course codes and full names a sentence happens to contain.

    Deliberately conservative, because this rewrites the owner's own words:

    - **Course codes only where a class note exists.** `_COURSE_CODE` matches the shape,
      the index decides whether it is real.
    - **Full names only.** A person note called "Will" would otherwise link every "will"
      in the corpus; requiring a space means the match is a first and last name, which is
      specific enough to be right.
    - **Never inside an existing link.** A value that already contains `[[` is left
      exactly as it is rather than nested into something Obsidian cannot parse.
    """
    if not text or "[[" in text:
        return text

    def link_course(match: re.Match[str]) -> str:
        raw = match.group(0)
        # "CHM113" and "CHM 113" are the same course written two ways; the ledger uses the
        # spaced form, so normalise before asking the index.
        parts = raw.split()
        spaced = raw if len(parts) == 2 else f"{raw[:-3]} {raw[-3:]}"
        path = index.course(spaced.strip())
        return _link(path, raw) if path else raw

    linked = _COURSE_CODE.sub(link_course, text)

    for name in sorted(index.people, key=len, reverse=True):
        if " " not in name or name.lower() != name:
            continue
        # The index holds each name twice, cased and lowercased; iterate the lowercase
        # keys and match case-insensitively so "Rachel Espericueta" and "rachel
        # espericueta" both land on the one note.
        pattern = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
        linked = _replace_outside_links(linked, pattern, index.people[name])

    return linked


def _replace_outside_links(text: str, pattern: re.Pattern[str], path: str) -> str:
    """Link every match that is not already inside a wikilink.

    Every occurrence, not the first, so this matches how course codes are handled a few
    lines up — Obsidian folds repeated links into one backlink anyway, and one rule for
    both kinds of mention is one fewer thing to remember. The span check is what makes it
    safe: a course code linked in the previous pass leaves `[[Classes/CHM 113|CHM 113]]`
    in the text, and a name inside that alias must not be linked a second time into
    something Obsidian cannot parse.
    """
    spans = [(m.start(), m.end()) for m in _WIKILINK.finditer(text)]
    out: list[str] = []
    cursor = 0
    for match in pattern.finditer(text):
        if any(start <= match.start() < end for start, end in spans):
            continue
        out.append(text[cursor : match.start()])
        out.append(_link(path, match.group(0)))
        cursor = match.end()
    out.append(text[cursor:])
    return "".join(out)


# ── the notes ────────────────────────────────────────────────────────────────


def _facts(
    conn: sqlite3.Connection, settings: Settings, index: Index
) -> tuple[Note, list[Note]]:
    """`Me.md` plus one note per subject.

    Read with SQL rather than through `facts.recall`, for one column: `recall` does not
    return `source_item_id`, and a knowledge base rendered without the link back to the
    document it came from is the exact thing rule 1 forbids.
    """
    rows = conn.execute(
        "SELECT id, subject, key, value, note, source, created_at, source_item_id"
        " FROM fact WHERE user_id = ? AND status = 'active'"
        " ORDER BY subject, key",
        (USER_ID,),
    ).fetchall()
    waiting = conn.execute(
        "SELECT subject, key, value, confidence, source_item_id FROM fact"
        " WHERE user_id = ? AND status = 'proposed' ORDER BY subject, key",
        (USER_ID,),
    ).fetchall()

    by_subject: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_subject.setdefault(str(row["subject"]), []).append(row)

    subject_notes: list[Note] = []
    for subject, items in sorted(by_subject.items()):
        body = [_front("facts", {"subject": subject}), f"# {subject}", ""]
        for row in items:
            # Linked here and not in Me.md's condensed list: the subject note is where a
            # fact is read in full, and it is the page Obsidian's backlink pane will name
            # when the owner is standing on a person or a class.
            line = f"- **{row['key']}** — {_mentions(str(row['value']), index)}"
            if row["note"]:
                line += f"  \n  _{row['note']}_"
            line += f"  \n  {_source(settings, row['source_item_id'])}"
            line += f" · recorded {str(row['created_at'])[:10]} via `{row['source']}`"
            body.append(line)
        body += ["", _link("Me.md", "back to Me")]
        subject_notes.append(
            Note(path=f"Facts/{safe_name(subject)}.md", body="\n".join(body) + "\n")
        )

    me = [
        _front("me"),
        "# Me",
        "",
        "What Backglass holds about the owner, from the `fact` table. Every line links to",
        "the document it was read out of; a line with no link was entered by hand.",
        "",
    ]
    if not by_subject:
        me.append("_Nothing recorded yet._")
    for subject, items in sorted(by_subject.items()):
        me += [f"## {subject}", ""]
        for row in items:
            cite = _source(settings, row["source_item_id"])
            me.append(f"- **{row['key']}** — {row['value']}  {cite}")
        me += ["", _link(f"Facts/{safe_name(subject)}.md", f"all of {subject}"), ""]

    if waiting:
        # Rule 2. These are candidates the extractor was not confident enough to accept,
        # so they are on the page as questions and never in the list above as facts.
        me += [
            "## Waiting on you",
            "",
            "Extracted but unconfirmed — not facts until accepted at `/memory`.",
            "",
        ]
        for row in waiting:
            confidence = row["confidence"]
            mark = f" · confidence {float(confidence):.2f}" if confidence is not None else ""
            me.append(
                f"- **{row['subject']} / {row['key']}** — {row['value']}"
                f"  {_source(settings, row['source_item_id'])}{mark}"
            )
        me.append("")

    return Note(path="Me.md", body="\n".join(me).rstrip() + "\n"), subject_notes


def _commitments(conn: sqlite3.Connection, settings: Settings, index: Index) -> Note:
    rows = list(
        conn.execute(
            query("open_commitments"),
            {"user_id": USER_ID, "confidence_threshold": settings.confidence_threshold},
        )
    )
    settled = [r for r in rows if not r["needs_review"]]
    unconfirmed = [r for r in rows if r["needs_review"]]

    body = [
        _front("commitments"),
        "# Open commitments",
        "",
        f"{len(settled)} open, oldest deadline first. Undated ones sink to the bottom,",
        "which is the ledger's own ordering and not a judgement about them.",
        "",
    ]

    def render_row(row: sqlite3.Row) -> str:
        arrow = "→" if row["direction"] == "i_owe" else "←"
        name = row["counterparty"]
        note = index.person(name)
        who = _link(note, str(name)) if note else (name or "unknown")
        what = _mentions(str(row["what"]), index)
        line = f"- {arrow} **{what}** — {who} · due {_day(row['due_at'])}"
        if row["evidence_quote"]:
            line += f'  \n  > {str(row["evidence_quote"]).strip()}'
        line += f"  \n  {_source(settings, row['source_item_id'])}"
        line += f" · {row['source']} · {_day(row['source_occurred_at'])}"
        return line

    if not settled:
        body.append("_Nothing open._")
    for row in settled:
        body.append(render_row(row))

    if unconfirmed:
        body += [
            "",
            "## Unconfirmed",
            "",
            f"{len(unconfirmed)} extraction(s) below the confidence threshold "
            f"({settings.confidence_threshold:.2f}). In the review queue, not in the brief,",
            "and not to be read as commitments until they are confirmed at `/review`.",
            "",
        ]
        body += [render_row(row) for row in unconfirmed]

    return Note(path="Commitments.md", body="\n".join(body).rstrip() + "\n")


def _people_rows(conn: sqlite3.Connection) -> tuple[list[tuple[sqlite3.Row, list[str]]], int]:
    """Everyone the ledger knows something about beyond a name, and how many it knows.

    The bound matters: `entity` holds every sender the extractor ever named, and a vault
    with four thousand near-empty person notes is a vault nobody opens. A person earns a
    note by having a role, an org, a tag, or an open commitment attached to them.

    Read before anything is rendered, because the link index is built out of it: a
    commitment can only link to a person note that this function decided to write.
    """
    rows = conn.execute(
        "SELECT e.id, e.canonical_name, e.kind, e.role, e.org, e.tags_json, e.notes,"
        " (SELECT COUNT(*) FROM commitment c"
        "   WHERE c.counterparty_entity_id = e.id AND c.status = 'open') AS open_n"
        " FROM entity e WHERE e.user_id = ? ORDER BY e.canonical_name",
        (USER_ID,),
    ).fetchall()

    kept = []
    for row in rows:
        tags = json.loads(row["tags_json"] or "[]")
        if row["role"] or row["org"] or tags or int(row["open_n"] or 0) > 0:
            kept.append((row, tags))
    return kept, len(rows)


def _people(
    conn: sqlite3.Connection,
    settings: Settings,
    kept: list[tuple[sqlite3.Row, list[str]]],
    known: int,
    index: Index,
) -> tuple[Note, list[Note]]:
    notes: list[Note] = []
    for row, tags in kept:
        name = str(row["canonical_name"])
        body = [_front("person", {"kind": str(row["kind"])}), f"# {name}", ""]
        if row["role"] or row["org"]:
            body.append(" · ".join(x for x in (row["role"], row["org"]) if x))
            body.append("")
        if tags:
            body += [" ".join(f"#{safe_name(str(t)).replace(' ', '-')}" for t in tags), ""]
        if row["notes"]:
            body += [str(row["notes"]), ""]

        open_rows = conn.execute(
            "SELECT id, what, due_at, direction, source_item_id FROM commitment"
            " WHERE user_id = ? AND counterparty_entity_id = ? AND status = 'open'"
            " ORDER BY due_at IS NULL, due_at, id",
            (USER_ID, int(row["id"])),
        ).fetchall()
        if open_rows:
            body += ["## Open between us", ""]
            for c in open_rows:
                arrow = "→" if c["direction"] == "i_owe" else "←"
                body.append(
                    f"- {arrow} {_mentions(str(c['what']), index)} · due {_day(c['due_at'])}"
                    f"  {_source(settings, c['source_item_id'])}"
                )
            body.append("")
        teaches = sorted(
            subject
            for subject, taught_by in index.taught_by.items()
            if name in taught_by
        )
        if teaches:
            body += ["## Teaches", ""]
            body += [_bullet(_link(index.classes[subject], subject)) for subject in teaches]
            body.append("")
        body.append(_link("People.md", "everyone"))
        notes.append(Note(path=f"People/{safe_name(name)}.md", body="\n".join(body) + "\n"))

    index = [
        _front("people"),
        "# People",
        "",
        f"{len(kept)} of {known} known names have more than a name attached.",
        "The rest are senders the extractor recorded and nothing else; they are in the",
        "ledger, not here.",
        "",
    ]
    for row, _tags in kept:
        name = str(row["canonical_name"])
        detail = " · ".join(x for x in (row["role"], row["org"]) if x)
        owed = int(row["open_n"] or 0)
        suffix = f" — {owed} open" if owed else ""
        index.append(
            _bullet(
                _link(f"People/{safe_name(name)}.md", name)
                + (f" — {detail}" if detail else "")
                + suffix
            )
        )
    if not kept:
        index.append("_Nobody yet._")

    return Note(path="People.md", body="\n".join(index) + "\n"), notes


def _classes(
    settings: Settings, loaded: list[Any], index: Index
) -> tuple[Note, list[Note]]:
    notes: list[Note] = []
    for course in loaded:
        body = [_front("class", {"subject": course.subject}), f"# {course.subject}", ""]
        if course.instructors:
            taught = []
            for name in course.instructors:
                note = index.person(name)
                taught.append(_link(note, name) if note else name)
            body += [f"**Taught by** {', '.join(taught)}", ""]
        if course.meetings:
            body += ["## Meets", ""]
            for meeting in course.meetings:
                room = f" · {meeting.room}" if meeting.room else ""
                body.append(f"- {meeting.component}: {meeting.when}{room}")
            body.append("")
        if course.dates:
            body += ["## Key dates", ""]
            for key_date in course.dates:
                body.append(
                    f"- **{key_date.title}** — {key_date.day_label} {key_date.time_label}"
                    f"  {_source(settings, key_date.source_item_id)}"
                )
            body.append("")
        if course.upcoming:
            body += ["## Next up", ""]
            for row in course.upcoming:
                due = _day(getattr(row, "due_at", None))
                body.append(f"- {getattr(row, 'title', '')} · due {due}")
            body.append("")
        if course.obligations:
            body += ["## Open in the ledger", ""]
            for obligation in course.obligations:
                minutes = (
                    f" · ~{obligation.estimated_minutes}m"
                    if obligation.estimated_minutes
                    else ""
                )
                body.append(
                    f"- {_mentions(str(obligation.what), index)}"
                    f" · due {_day(obligation.due_at)}{minutes}"
                    f"  {_source(settings, obligation.source_item_id)}"
                )
            body.append("")
        if course.documents:
            body += ["## Documents", ""]
            for doc in course.documents:
                body.append(
                    f"- {doc.title} — `{doc.path}`  {_source(settings, doc.source_item_id)}"
                )
            body.append("")
        body.append(_link("Classes.md", "all classes"))
        notes.append(
            Note(path=f"Classes/{safe_name(course.subject)}.md", body="\n".join(body) + "\n")
        )

    index = [_front("classes"), "# Classes", ""]
    if not loaded:
        index.append("_No semester in the ledger yet._")
    for course in loaded:
        nxt = course.next_meeting
        when = f" — next {nxt.when}" if nxt else ""
        index.append(
            _bullet(
                _link(f"Classes/{safe_name(course.subject)}.md", course.subject)
                + when
                + f" · {course.assignment_total} assignment(s)"
            )
        )
    return Note(path="Classes.md", body="\n".join(index) + "\n"), notes


def _questions(conn: sqlite3.Connection) -> Note:
    from backglass import questions as questions_mod

    rows = questions_mod.open_questions(conn)
    body = [
        _front("questions"),
        "# Open questions",
        "",
        "What Backglass could not settle from the evidence, and asked instead of guessing.",
        "Answer them at `/ask`; answering here does nothing, because this file is a report.",
        "",
    ]
    if not rows:
        body.append("_Nothing waiting._")
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_kind.setdefault(str(row["kind"]), []).append(row)
    for kind, items in sorted(by_kind.items()):
        body += [f"## {kind} ({len(items)})", ""]
        for row in items:
            body.append(f"- {row['question']} _(asked {str(row['asked_at'])[:10]})_")
            if row.get("detail"):
                body.append(f"  \n  {str(row['detail']).strip()}")
        body.append("")
    return Note(path="Questions.md", body="\n".join(body).rstrip() + "\n")


def _decisions(conn: sqlite3.Connection) -> Note:
    from backglass import decisions as decisions_mod

    rows = decisions_mod.active(conn)
    body = [
        _front("decisions"),
        "# Decisions",
        "",
        "Choices the owner has settled. Not commitments — nothing is left to do — and not",
        "facts, because each one has a date and a reason behind it.",
        "",
    ]
    if not rows:
        body.append("_Nothing recorded._")
    for decision in rows:
        line = f"- **{decision.title}**: {decision.choice}"
        if decision.reasoning:
            line += f"  \n  _{decision.reasoning}_"
        line += f"  \n  decided {decision.decided_at[:10]}"
        if decision.commitment_title:
            verb = "closed" if decision.closed_commitment else "relates to"
            line += f" · {verb} “{decision.commitment_title}”"
        body.append(line)
    return Note(path="Decisions.md", body="\n".join(body) + "\n")


def _goals(conn: sqlite3.Connection, settings: Settings, now: datetime) -> Note:
    from backglass.goals import targets as targets_mod

    rows = targets_mod.progress(conn, settings, now.date())
    body = [
        _front("goals"),
        "# Goals",
        "",
        "Every count here comes from checkpoints; nothing is entered directly.",
        "",
    ]
    if not rows:
        body.append("_No targets set._")
    by_goal: dict[str, list[Any]] = {}
    for row in rows:
        by_goal.setdefault(row.goal_title, []).append(row)
    for goal, items in sorted(by_goal.items()):
        body += [f"## {goal}", ""]
        for target in items:
            state = "done" if target.complete else "open"
            if target.kind == "periodic":
                detail = f"{target.chip()} · every {target.every_days} days"
            elif target.kind == "total":
                detail = f"{target.lifetime_done} of {target.total_count or '?'}"
            else:
                detail = f"{target.done_this_week} of {target.weekly_count or 0} this week"
            missed = f" · missed {target.missed_weeks} week(s)" if target.missed_weeks else ""
            body.append(f"- **{target.title}** — {detail} ({state}){missed}")
        body.append("")
    return Note(path="Goals.md", body="\n".join(body).rstrip() + "\n")


def _sources(conn: sqlite3.Connection) -> Note:
    rows = conn.execute(
        "SELECT source, COUNT(*) AS n,"
        " SUM(CASE WHEN triage_verdict = 'keep' THEN 1 ELSE 0 END) AS kept,"
        " MAX(occurred_at) AS latest"
        " FROM source_item WHERE user_id = ? GROUP BY source ORDER BY n DESC, source",
        (USER_ID,),
    ).fetchall()
    body = [
        _front("sources"),
        "# Sources",
        "",
        "What is in the ledger and where it came from. Counts, not health: a connector",
        "that is failing right now says so in the dashboard's Sources panel and in",
        "`backglass state`, which is the surface that reads live.",
        "",
        "| source | items | kept | most recent |",
        "|---|---:|---:|---|",
    ]
    for row in rows:
        body.append(
            f"| `{row['source']}` | {row['n']} | {row['kept'] or 0} | {_day(row['latest'])} |"
        )
    if not rows:
        body.append("| _nothing ingested_ | 0 | 0 | — |")
    return Note(path="Sources.md", body="\n".join(body) + "\n")


#: How many drop-folder documents `Documents.md` lists individually. The archive can grow
#: without limit and a note nobody scrolls to the end of is a note that lies by omission,
#: so the cap is stated in the file whenever it bites.
DOCUMENT_LIMIT = 200


def _documents(conn: sqlite3.Connection, settings: Settings) -> Note:
    """The evidence pile, and the honest answer about how to search it.

    This is the one place the vault touches retrieval, and it touches it by pointing
    somewhere else. Semantic search is additive and lives in `search.py`; a markdown file
    cannot run a cosine, and faking one here — a hand-picked list of "relevant" documents
    — would be the search-box-over-a-pile the product rejects, wearing a vault's clothes.

    What a note *can* do is name the documents the drop folder holds, link each to its
    page, and say what each one produced. A contract that yielded three facts and a
    deadline is a different thing from one that yielded nothing, and that difference is
    invisible in a folder listing.
    """
    rows = conn.execute(
        "SELECT id, title, occurred_at FROM source_item"
        " WHERE user_id = ? AND triage_verdict = 'keep' AND source = 'files'"
        " ORDER BY occurred_at DESC, id DESC",
        (USER_ID,),
    ).fetchall()

    body = [
        _front("documents"),
        "# Documents",
        "",
        "The drop folder, once it has been read. Each document links to its page, and to",
        "what it turned into — a document that produced nothing is worth knowing about",
        "too.",
        "",
    ]

    try:
        from backglass import search as search_mod

        stats = search_mod.coverage(conn, settings)
        body += [
            f"Searchable: {stats['indexed']} of {stats['indexable']} kept documents are"
            f" indexed ({stats['model']}), {stats['pending']} pending. Search is a command,",
            "not a note — `backglass search find \"which letter mentioned the deposit\"` —",
            "because retrieval ranks the whole corpus and a file can only list part of it.",
            "",
        ]
    except Exception:  # noqa: BLE001 — rule 5: the vault renders without retrieval
        body += [
            "Searchable: unknown — the retrieval index could not be read. Every other",
            "line in this vault is unaffected; search is additive by design.",
            "",
        ]

    if not rows:
        body.append("_Nothing in the drop folder has been ingested yet._")
        return Note(path="Documents.md", body="\n".join(body) + "\n")

    shown = rows[:DOCUMENT_LIMIT]
    if len(rows) > len(shown):
        body += [
            f"Listing the {len(shown)} most recent of {len(rows)}. The rest are in the",
            "ledger and reachable by search; they are not missing, only unlisted.",
            "",
        ]

    for row in shown:
        source_id = int(row["id"])
        body += [f"## {row['title'] or 'untitled'}", ""]
        body.append(f"{_day(row['occurred_at'])} · {_source(settings, source_id)}")
        facts = conn.execute(
            "SELECT subject, key, value FROM fact"
            " WHERE user_id = ? AND source_item_id = ? AND status = 'active'"
            " ORDER BY subject, key",
            (USER_ID, source_id),
        ).fetchall()
        commitments = conn.execute(
            "SELECT what, due_at FROM commitment"
            " WHERE user_id = ? AND source_item_id = ? AND status = 'open'"
            " ORDER BY due_at IS NULL, due_at, id",
            (USER_ID, source_id),
        ).fetchall()
        if not facts and not commitments:
            body += ["", "_Read, and stated nothing the ledger keeps._", ""]
            continue
        body.append("")
        for fact in facts:
            lane = _link(f"Facts/{safe_name(str(fact['subject']))}.md", str(fact["subject"]))
            body.append(f"- {lane} · **{fact['key']}** — {fact['value']}")
        for commitment in commitments:
            body.append(f"- open: {commitment['what']} · due {_day(commitment['due_at'])}")
        body.append("")

    return Note(path="Documents.md", body="\n".join(body).rstrip() + "\n")


def _told(conn: sqlite3.Connection, settings: Settings) -> str:
    """The context block every model call carries, verbatim.

    The most useful thing this vault can say about the pipeline is what the pipeline
    knows. `context.assemble` is what triage and extraction are handed before they read a
    single message, and until it was written down here the only way to see it was to add a
    print statement — so a tier quietly rendering empty (no decisions, no semester, a
    people section that aged out) looked exactly like a tier working.

    Verbatim rather than summarised, because a second wording of the block is a second
    thing that can drift from it. Best-effort (rule 5): a context module that raises must
    not take the vault with it.
    """
    try:
        from backglass import context as context_mod

        return context_mod.assemble(conn, settings)
    except Exception as exc:  # noqa: BLE001 — rule 5, and the reason is reportable
        return f"__unavailable__ {type(exc).__name__}: {exc}"


def _state(state: Any, verdicts: list[Any], now: datetime, told: str = "") -> Note:
    """The evolving check-against document.

    Deliberately the same claims `backglass state` prints, in the same order, with the
    same derivations attached — a second wording of ground truth is a second thing to
    keep true. What this adds is that it survives the terminal scrollback and sits beside
    the rest of the vault, so "is what I am looking at current" has an answer in the same
    window as the thing being looked at.
    """
    body = [
        _front("state", {"generated_at": now.isoformat(timespec="seconds")}),
        "# State",
        "",
        f"Snapshot taken {now.strftime('%Y-%m-%d %H:%M')}. Everything below was read, not",
        "remembered. Re-read it with `uv run backglass state` — this file is that command,",
        "written down, and it is stale the moment something changes.",
        "",
        "## Checks",
        "",
    ]
    for verdict in verdicts:
        mark = "?" if getattr(verdict, "unknown", False) else ("ok" if verdict.ok else "FAIL")
        line = f"- **[{mark}]** {verdict.name}"
        if verdict.detail:
            line += f" — {verdict.detail}"
        if not verdict.ok and verdict.remedy:
            line += f"  \n  fix: `{verdict.remedy}`"
        body.append(line)

    body += ["", "## Claims", ""]
    for section, claims in state.sections.items():
        body += [f"### {section}", ""]
        for name, claim in claims.items():
            if claim.unknown:
                body.append(f"- **{name}**: _unknown_ — {claim.unknown}")
                continue
            value = claim.value
            if isinstance(value, (dict, list)):
                value = json.dumps(value, sort_keys=True, default=str)
            text = str(value)
            if len(text) > 400:
                text = text[:400] + "…"
            body.append(f"- **{name}**: {text}  \n  _via {claim.how}_")
        body.append("")

    body += ["## What every model call is told", ""]
    if not told:
        body += [
            "Nothing. `context.assemble` renders empty, which on a ledger with facts in it",
            "means every tier came back blank — worth finding out rather than assuming.",
            "",
        ]
    elif told.startswith("__unavailable__"):
        body += [
            f"_Unknown_ — {told.removeprefix('__unavailable__ ')}. The block could not be",
            "assembled, so what the pipeline knows right now cannot be stated here.",
            "",
        ]
    else:
        body += [
            "Verbatim, the block triage and extraction carry before they read a message.",
            "A tier missing here is a tier the model is deciding without.",
            "",
            "```",
            told,
            "```",
            "",
        ]
    return Note(path="STATE.md", body="\n".join(body).rstrip() + "\n")


def _home(counts: dict[str, int]) -> Note:
    body = [
        _front("home"),
        "# Backglass",
        "",
        "This vault is written by Backglass out of the ledger. Every file except the ones",
        "in `Inbox/` is regenerated by `backglass vault export`, so editing them is",
        "editing something that is about to be overwritten — change the ledger instead.",
        "",
        "Start with " + _link("STATE.md") + " if you want to know whether what you are",
        "reading is current.",
        "",
        "## The record",
        "",
        _bullet(
            _link("Me.md")
            + f" — {counts.get('facts', 0)} durable facts"
            + f" across {counts.get('subjects', 0)} subjects"
        ),
        _bullet(_link("Commitments.md") + f" — {counts.get('commitments', 0)} open"),
        _bullet(_link("Classes.md") + f" — {counts.get('classes', 0)} this semester"),
        _bullet(_link("People.md") + f" — {counts.get('people', 0)} with more than a name"),
        _bullet(_link("Goals.md")),
        _bullet(_link("Decisions.md")),
        _bullet(_link("Questions.md") + f" — {counts.get('questions', 0)} waiting on you"),
        _bullet(
            _link("Documents.md")
            + f" — {counts.get('documents', 0)} from the drop folder, and how to search"
        ),
        _bullet(_link("Sources.md") + f" — {counts.get('items', 0)} items ingested"),
        "",
        "## Your half",
        "",
        "`Inbox/` is yours. Notes you write there are read back into the ledger on the next",
        "sync, triaged and extracted like anything else — so a note that says \"pay housing",
        'by the 30th" becomes a commitment with this vault as its source. Nothing Backglass',
        "generates is ever read back in; those files are marked `backglass: generated` and",
        "the connector skips them, which is what stops the record feeding on itself.",
        "",
    ]
    return Note(path="Backglass.md", body="\n".join(body) + "\n")


def _inbox_readme() -> Note:
    body = [
        _front("inbox"),
        "# Inbox",
        "",
        "Write notes here. The next sync reads this folder, triages what you wrote, and",
        "extracts commitments and dates out of it the same way it does an email.",
        "",
        "Two things worth knowing:",
        "",
        "- **Date your notes.** A `date:` in the frontmatter is what relative wording",
        '  resolves against, so "by Friday" in a note dated three weeks ago is that',
        "  Friday, not this one.",
        "- **This file is generated**, which is why it is not ingested. Yours will be.",
        "",
    ]
    return Note(path="Inbox/README.md", body="\n".join(body) + "\n")


# ── the export ───────────────────────────────────────────────────────────────


def render(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    now: datetime,
    snapshot: tuple[Any, list[Any]] | None = None,
) -> list[Note]:
    """Every note the vault should contain, as text. Writes nothing.

    Pure by construction so the idempotency test can render twice and compare bytes
    without touching a disk, and so a caller that wants to diff before writing can.
    """
    from backglass import courses as courses_mod

    # Read what exists before writing anything that links to it. The index is the whole
    # reason this is two passes: a commitment naming Rachel Espericueta should link to her
    # note when there is one, and stay plain text when there is not.
    kept_people, known_people = _people_rows(conn)
    loaded_classes = courses_mod.load(conn, settings, now=now)
    index = _index(kept_people, loaded_classes)

    notes: list[Note] = []

    me, fact_notes = _facts(conn, settings, index)
    notes.append(me)
    notes += fact_notes

    notes.append(_commitments(conn, settings, index))

    people_index, people_notes = _people(conn, settings, kept_people, known_people, index)
    notes.append(people_index)
    notes += people_notes

    classes_index, class_notes = _classes(settings, loaded_classes, index)
    notes.append(classes_index)
    notes += class_notes

    notes.append(_goals(conn, settings, now))
    notes.append(_questions(conn))
    notes.append(_decisions(conn))
    notes.append(_documents(conn, settings))
    notes.append(_sources(conn))
    notes.append(_inbox_readme())

    if snapshot is None:
        from backglass import state as state_mod

        collected = state_mod.collect(conn, settings)
        snapshot = (collected, state_mod.verdicts(collected, conn, settings))
    notes.append(_state(snapshot[0], snapshot[1], now, _told(conn, settings)))

    counts = {
        "subjects": len(fact_notes),
        "facts": _count(
            conn, "SELECT COUNT(*) AS n FROM fact WHERE user_id = ? AND status = 'active'"
        ),
        "commitments": _count(
            conn, "SELECT COUNT(*) AS n FROM commitment WHERE user_id = ? AND status = 'open'"
        ),
        "classes": len(class_notes),
        "people": len(people_notes),
        "questions": _count(
            conn,
            "SELECT COUNT(*) AS n FROM open_question WHERE user_id = ? AND status = 'open'",
        ),
        "documents": _count(
            conn,
            "SELECT COUNT(*) AS n FROM source_item WHERE user_id = ?"
            " AND triage_verdict = 'keep' AND source = 'files'",
        ),
        "items": _count(conn, "SELECT COUNT(*) AS n FROM source_item WHERE user_id = ?"),
    }
    notes.append(_home(counts))

    return sorted(notes, key=lambda note: note.path)


def _index(
    kept_people: list[tuple[sqlite3.Row, list[str]]], loaded_classes: list[Any]
) -> Index:
    """Which notes are about to exist, keyed the ways a mention arrives.

    Names are held twice, cased and lowercased, because the ledger is inconsistent about
    it — an entity's `canonical_name` is whatever the extractor read, and a commitment's
    counterparty comes through the same column while a fact's sentence does not.
    """
    people: dict[str, str] = {}
    for row, _tags in kept_people:
        name = str(row["canonical_name"])
        path = f"People/{safe_name(name)}.md"
        people[name] = path
        people.setdefault(name.lower(), path)

    classes: dict[str, str] = {}
    taught_by: dict[str, list[str]] = {}
    for course in loaded_classes:
        subject = str(course.subject)
        classes[subject] = f"Classes/{safe_name(subject)}.md"
        taught_by[subject] = [str(name) for name in course.instructors]

    return Index(people=people, classes=classes, taught_by=taught_by)


def _count(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql, (USER_ID,)).fetchone()
    return int(row["n"]) if row else 0


def _prune(root: Path, keep: set[str], report: Report, *, dry_run: bool) -> None:
    """Delete generated notes the ledger has stopped producing.

    Without this the vault only ever grows, and what it grows is stale claims: a person
    whose last open commitment closed drops out of the render and their note sits there
    saying they owe something. Rule 1 is that every claim links to its source, and a file
    no longer backed by a query has no source left to link to.

    Two conditions, both required, because deleting the owner's writing would be far
    worse than keeping a stale report. The file must carry the generated marker, and it
    must sit where the export writes — `OWNED_DIRS`, or the vault root. A note the owner
    wrote in `Facts/` survives on the first condition; a generated note they moved to
    `Archive/` survives on the second.
    """
    if not root.is_dir():
        return
    marker = f"{MARK_KEY}: {MARK_VALUE}"
    for path in sorted(root.rglob("*.md")):
        try:
            relative = path.relative_to(root)
        except ValueError:  # pragma: no cover — rglob cannot leave the root
            continue
        posix = relative.as_posix()
        if posix in keep:
            continue
        owned = len(relative.parts) == 1 or relative.parts[0] in OWNED_DIRS
        if not owned:
            continue
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:200]
        except OSError:
            continue
        if marker not in head:
            continue
        if dry_run:
            report.removed.append(posix)
            continue
        try:
            path.unlink()
        except OSError as exc:
            report.failed.append(f"{posix}: {exc}")
            continue
        report.removed.append(posix)


@dataclass(frozen=True)
class Status:
    """What is on disk right now, cheaply enough to ask on every page render.

    Deliberately lighter than `state._vault`, which opens the head of every note to count
    the generated marker. That count is the safety number and it belongs in `backglass
    state`; this one answers the question /memory needs — is there a vault, how much is in
    it, and when was it last written — with one directory walk and one file read.
    """

    root: Path | None = None
    #: None when the root is configured but absent, which is a different fact from zero.
    notes: int | None = None
    exported_at: str | None = None
    #: True when the vault is also an ingest source, so the page can say that the loop is
    #: closed by the generated marker rather than leaving it to be discovered.
    also_ingested: bool = False


def status(settings: Settings) -> Status:
    """The vault as the page needs it. Never raises: a missing or unreadable vault is a
    sentence on the page, not a 500 on a page about something else (rule 5)."""
    configured = settings.vault_export_path
    if not configured:
        return Status()
    root = Path(configured).expanduser()
    ingested = settings.obsidian_vault_path
    also = ingested is not None and Path(ingested).expanduser() == root
    if not root.is_dir():
        return Status(root=root, also_ingested=also)
    try:
        notes = sum(1 for path in root.rglob("*.md") if ".obsidian" not in path.parts)
    except OSError:
        notes = None
    return Status(root=root, notes=notes, exported_at=_exported_at(root), also_ingested=also)


def _exported_at(root: Path) -> str | None:
    """`STATE.md`'s own `generated_at`. It is the only note that carries a timestamp —
    every other note deliberately does not, so that a re-export with nothing to say
    rewrites nothing (rule 3)."""
    try:
        head = (root / "STATE.md").read_text(encoding="utf-8", errors="replace")[:400]
    except OSError:
        return None
    for line in head.splitlines():
        if line.startswith("generated_at:"):
            return line.split(":", 1)[1].strip() or None
    return None


def export(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    root: Path,
    now: datetime,
    dry_run: bool = False,
    snapshot: tuple[Any, list[Any]] | None = None,
) -> Report:
    """Write the vault. A note whose bytes are unchanged is left alone.

    That last clause is the whole idempotency story (rule 3) and also the sync story: a
    rewritten file gets a new mtime, and `connectors/notes.py` watermarks on mtime, so
    rewriting 400 unchanged notes would hand the next sync 400 files to re-read and
    discard. Comparing bytes first costs one read and saves all of it.
    """
    report = Report(root=root)
    notes = render(conn, settings, now=now, snapshot=snapshot)
    _prune(root, {note.path for note in notes}, report, dry_run=dry_run)
    for note in notes:
        target = root / note.path
        try:
            existing = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            existing = None
        if existing == note.body:
            report.unchanged.append(note.path)
            continue
        if dry_run:
            report.written.append(note.path)
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(note.body, encoding="utf-8")
        except OSError as exc:
            # Rule 5. A vault on an unmounted drive must not take the sync down with it.
            report.failed.append(f"{note.path}: {exc}")
            continue
        report.written.append(note.path)
    return report
