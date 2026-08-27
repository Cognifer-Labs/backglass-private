"""The evolving state doc: who the owner is right now, and what changed to make it so.

The owner, 2026-08-20: *"make the logic engine better at understanding current situation
and create an evolving state doc about me."*

**This is a rendering, not a store, and that is the load-bearing decision.** The `fact`
table already is the claims layer about the owner — supersession, citations, status,
confidence, and a page they curate it on. "Enrolled at ASU Tempe" is literally fact row 5.
A second table holding owner-state would drift from the first within a month, and
CLAUDE.md's ledger-stays-primary rule is what forbids it. So `situation_doc` (0033) stores
the rendered body and its hash, nothing else, and nothing in the pipeline reads the stored
body to decide anything: every reader calls `render()` against the live ledger.

**The evolution is free, and it is the diff.** A new version is written only when the body
hash changes (rule 3), so facts supersede, the next rendering differs, and the change is
legible without anyone maintaining a changelog. `versions()` is the history; `changes()`
is what moved between two of them.

**Why this is not `facts.owner_context`.** That block renders the same rows for prompts
that only need to read them, capped and without ids. This one is addressable — every line
carries `[fact N]` — because a `claim_dependency` has to point at a claim that has
provenance, and a doc whose claims cannot be cited would turn every drop it justifies into
an opinion. Rule 1 holds inside the document, not just around it.

**Why the body carries no date.** Everything the doc says is relative to a day, and an
"as of 2026-08-24" line in the body would change the hash at every midnight — the version
list would become a log of how often the job ran, which `save()` exists to prevent. The
day a version was rendered is `created_at`, which is where it belongs. What is inside the
sections still moves with the calendar, and that movement is real: an obligation crossing
into overdue *is* a change to the owner's situation and has earned its version.

Four sections, in the order a stranger would need them: who the owner is, what recently
stopped being true, what is happening this week, and what the open obligations rest on.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID

#: How far back "what changed recently" reaches. A superseded fact is the thing that
#: retires an obligation, so this window is what the reader needs to see to understand why
#: the board looks different than it did — not the whole history, which the Memory page
#: already holds.
CHANGE_WINDOW_DAYS = 60

#: How many facts the dependency section names before it falls back to a count. The
#: section answers "what is my board resting on"; a two-hundred-line table answers nothing.
RESTS_ON_LIMIT = 10

#: Header for the week section. `context._situation` writes its own, dated, header; this
#: replaces it for the reason the module docstring gives — a date in the body is a hash
#: that changes at midnight for no change in the ledger.
NOW_HEADER = "WHAT IS HAPPENING THIS WEEK (open commitments, plans and questions)"


@dataclass(frozen=True)
class Version:
    """One stored rendering."""

    version_id: int
    body: str
    body_hash: str
    created_at: str


# ── rendering ──────────────────────────────────────────────────────────────


def render(
    conn: sqlite3.Connection,
    settings: Settings,
    day: date,
    *,
    include_facts: bool = True,
) -> str:
    """The whole document, deterministic for a given ledger and day.

    Deterministic matters twice here. It is what makes `save` idempotent — a body that
    reordered itself between calls would write a new version every sync and the diff would
    become noise — and it is what lets this block be handed to a caching backend when the
    relevance pass carries it.

    `include_facts=False` omits the WHO section. It exists for exactly one caller: the
    relevance prompt already carries the active facts under its own header, addressable in
    the same `[fact N]` form, because that list is the citation contract a `nonsense`
    verdict is validated against. Rendering them a second time inside the doc would put
    two copies of the same claims in one prompt and leave the model to guess which list to
    cite from. What that caller does not have — and what this pass exists to give it — is
    the other three sections. The stored versions always include the facts; a document
    about the owner that omits who they are is not the document.
    """
    sections = [
        _who(conn) if include_facts else "",
        _changed(conn, day),
        _now(conn, settings, day),
        _rests_on(conn),
    ]
    body = "\n\n".join(s for s in sections if s)
    return f"{body}\n" if body else ""


def _who(conn: sqlite3.Connection) -> str:
    """Every active fact, addressable, grouped by the lane the owner filed it under.

    Uncapped, unlike `facts.owner_context`. This is a document a person reads and a
    dependency cites; truncating it would silently make some claims uncitable, and which
    ones would depend on how long the values happened to be.
    """
    rows = conn.execute(
        "SELECT id, subject, key, value FROM fact"
        " WHERE user_id = ? AND status = 'active' ORDER BY subject, key, id",
        (USER_ID,),
    ).fetchall()
    if not rows:
        return ""
    lines: list[str] = []
    subject = None
    for r in rows:
        if r["subject"] != subject:
            subject = str(r["subject"])
            lines.append(f"  {subject}")
        lines.append(f"    [fact {r['id']}] {r['key']}: {r['value']}")
    return "WHO THE OWNER IS (recorded facts, each addressable)\n" + "\n".join(lines)


def _changed(conn: sqlite3.Connection, day: date) -> str:
    """Facts that stopped being true lately, with what replaced them.

    This is the section the goal turns on. A superseded fact is precisely the event that
    can retire an obligation — enrolling somewhere retires every other university's
    paperwork at once — so the document has to show the move, not just the current value.
    Retractions are shown too, and named as such: a fact the owner pulled back is a
    different event from one that was overtaken, and collapsing them would hide which of
    the two happened.

    Read from the `fact` table rather than from `claim_event`, deliberately. The change
    ledger (0032) is the better long-run source and is where the redesign points this
    section, but it only began recording on 2026-08-21, while `superseded_by` reaches
    back over the whole history of the ledger. A "what changed in the last 60 days"
    section sourced from a 3-day-old log would be silently, confidently short. When the
    ledger's depth exceeds this window, this query is the thing to replace.
    """
    since = (day - timedelta(days=CHANGE_WINDOW_DAYS)).isoformat()
    rows = conn.execute(
        """
        SELECT old.id          AS old_id,
               old.subject     AS subject,
               old.key         AS key,
               old.value       AS old_value,
               old.status      AS status,
               new.id          AS new_id,
               new.value       AS new_value,
               COALESCE(new.created_at, old.created_at) AS changed_at
          FROM fact old
          LEFT JOIN fact new ON new.id = old.superseded_by
         WHERE old.user_id = ? AND old.status IN ('superseded', 'retracted')
           AND substr(COALESCE(new.created_at, old.created_at), 1, 10) >= ?
         ORDER BY changed_at DESC, old.id DESC
        """,
        (USER_ID, since),
    ).fetchall()
    if not rows:
        return ""
    lines = []
    for r in rows:
        when = str(r["changed_at"])[:10]
        head = f"    {when} · {r['subject']}/{r['key']}"
        if r["new_id"] is not None:
            lines.append(
                f"{head}: [fact {r['old_id']}] \"{r['old_value']}\""
                f" → [fact {r['new_id']}] \"{r['new_value']}\""
            )
        else:
            lines.append(
                f"{head}: [fact {r['old_id']}] \"{r['old_value']}\" was"
                f" {r['status']}, with nothing recorded in its place"
            )
    return (
        f"WHAT CHANGED IN THE LAST {CHANGE_WINDOW_DAYS} DAYS"
        " (superseded or retracted facts)\n" + "\n".join(lines)
    )


def _now(conn: sqlite3.Connection, settings: Settings, day: date) -> str:
    """This week, from `context._situation` rather than a second copy of it.

    Called, not reimplemented. That report already handles the thing a reimplementation
    would get wrong — `substr(due_at, 1, 10)` instead of `date()`, because the ledger mixes
    bare dates, naive locals and offset-bearing datetimes and only the leading ten
    characters mean the local day under all three (lessons, 2026-08-01).

    Its dated header is replaced with an undated one. See the module docstring: the day
    belongs to the version's `created_at`, not to bytes that are hashed.
    """
    from backglass import context

    block = context._situation(conn, day, limit=context.NOW_CHARS * 3)
    if not block:
        return ""
    _, _, rest = block.partition("\n")
    return f"{NOW_HEADER}\n{rest}" if rest else ""


def _rests_on(conn: sqlite3.Connection) -> str:
    """What the open board is standing on, counted by the fact holding it up.

    The section exists so a wrong dependency is visible *before* it deletes anything. If
    one fact is carrying forty obligations, that is either the truth about the owner's life
    or a pass that over-cited, and either way the owner should see it on a page rather than
    discover it the day the fact is superseded.

    Reads `claim_dependency` (0032), which is goal 3's `commitment_dependency` generalized
    past commitment to any subject table; this section asks only about commitments, since
    those are the rows an invalidation would close. **An empty section means "nothing has
    recorded a dependency yet", never "nothing depends on anything"** — that is the rule
    `claim_events.py` states about its own table, and it holds here: as of 0033 no pass
    writes dependencies, so this renders empty on every real ledger until relevance v2
    lands. It is built now because the doc is where a wrong dependency has to become
    visible, and a section added after the writer is a section nobody reads the first
    time it matters.
    """
    rows = conn.execute(
        """
        SELECT d.fact_id AS fact_id, f.subject AS subject, f.key AS key,
               f.status AS fact_status, COUNT(*) AS n
          FROM claim_dependency d
          JOIN commitment c ON c.id = d.subject_id AND c.status = 'open'
          JOIN fact f       ON f.id = d.fact_id
         WHERE d.user_id = ? AND d.status = 'active' AND d.kind = 'fact'
           AND d.subject_table = 'commitment'
         GROUP BY d.fact_id, f.subject, f.key, f.status
         ORDER BY n DESC, d.fact_id ASC
        """,
        (USER_ID,),
    ).fetchall()
    totals = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM commitment
            WHERE user_id = :u AND status = 'open') AS open_count,
          (SELECT COUNT(DISTINCT subject_id) FROM claim_dependency
            WHERE user_id = :u AND status = 'active'
              AND subject_table = 'commitment') AS judged,
          (SELECT COUNT(*) FROM claim_dependency
            WHERE user_id = :u AND status = 'active' AND kind = 'none'
              AND subject_table = 'commitment') AS standalone
        """,
        {"u": USER_ID},
    ).fetchone()
    if not rows and not (totals and totals["judged"]):
        return ""

    lines = []
    for r in rows[:RESTS_ON_LIMIT]:
        gone = (
            "" if r["fact_status"] == "active" else f" — NO LONGER {r['fact_status'].upper()}"
        )
        lines.append(
            f"    {r['n']} open · [fact {r['fact_id']}] {r['subject']}/{r['key']}{gone}"
        )
    if len(rows) > RESTS_ON_LIMIT:
        lines.append(f"    … and {len(rows) - RESTS_ON_LIMIT} more facts with dependents")
    if totals:
        lines.append(
            f"    {totals['judged']} of {totals['open_count']} open commitments judged;"
            f" {totals['standalone']} rest on no recorded fact"
        )
    return "WHAT THE OPEN OBLIGATIONS REST ON\n" + "\n".join(lines)


# ── versions ───────────────────────────────────────────────────────────────


def _hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def save(conn: sqlite3.Connection, body: str) -> Version | None:
    """Store a rendering, or nothing if it says exactly what the last one said.

    Rule 3 lives on this line. Two syncs over an unchanged ledger produce the same body,
    the same hash, and no write — and the version list stays a list of *changes* rather
    than a log of how often the job ran, which is the only form in which it is readable.
    """
    if not body:
        return None
    digest = _hash(body)
    current = latest(conn)
    if current is not None and current.body_hash == digest:
        return None
    stamp = now_iso()
    cursor = conn.execute(
        "INSERT INTO situation_doc (user_id, body, body_hash, created_at)"
        " VALUES (?, ?, ?, ?)",
        (USER_ID, body, digest, stamp),
    )
    return Version(
        version_id=int(cursor.lastrowid or 0),
        body=body,
        body_hash=digest,
        created_at=stamp,
    )


def latest(conn: sqlite3.Connection) -> Version | None:
    row = conn.execute(
        "SELECT id, body, body_hash, created_at FROM situation_doc"
        " WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (USER_ID,),
    ).fetchone()
    return _version(row) if row else None


def versions(conn: sqlite3.Connection, limit: int = 20) -> list[Version]:
    """Newest first — the document's history, which is to say its evolution."""
    return [
        _version(row)
        for row in conn.execute(
            "SELECT id, body, body_hash, created_at FROM situation_doc"
            " WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (USER_ID, limit),
        )
    ]


def _version(row: Any) -> Version:
    return Version(
        version_id=int(row["id"]),
        body=str(row["body"]),
        body_hash=str(row["body_hash"]),
        created_at=str(row["created_at"]),
    )


def changes(previous: Version | None, current: Version) -> list[str]:
    """The lines that appeared and vanished between two versions, `+`/`-` prefixed.

    A line diff rather than a word diff, because every line of this document is one
    addressable claim: "[fact 5] enrolment: ASU Tempe" leaving and a new one arriving is
    the event, and a word-level diff would render it as characters changing inside a
    sentence.
    """
    if previous is None:
        return [f"+ {line.strip()}" for line in current.body.splitlines() if line.strip()]
    before = previous.body.splitlines()
    after = current.body.splitlines()
    gone = [line for line in before if line.strip() and line not in after]
    came = [line for line in after if line.strip() and line not in before]
    return [f"- {line.strip()}" for line in gone] + [f"+ {line.strip()}" for line in came]


def refresh(
    conn: sqlite3.Connection, settings: Settings, day: date
) -> tuple[Version | None, list[str]]:
    """Render, store if it moved, and say what moved. The whole surface in one call."""
    previous = latest(conn)
    version = save(conn, render(conn, settings, day))
    if version is None:
        return None, []
    return version, changes(previous, version)
