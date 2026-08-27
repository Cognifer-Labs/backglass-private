"""The full picture, assembled for a model call: memory in five tiers.

Every model call in this pipeline used to read one message knowing almost nothing about
the person it was reading for. Triage got the fact table (facts.owner_context, v3);
extraction got a name and two email addresses. Nobody got the people, and nobody got the
situation — so "can you send that to Priya by Friday" was read by a model that did not
know who Priya is, that three other things are due Friday, or that the owner is mid-move
between two cities the fact table has known about for weeks.

This module assembles that picture. Three tiers, in the order a stranger would need them:

  1. **Long-term memory** — the fact table, verbatim from `facts.owner_context`. Durable,
     owner-curated, already deterministic and capped.
  2. **What has already been settled** — the standing decisions. Added 2026-08-23, for a
     failure the other four tiers cannot see: the ledger knew the owner had declined an
     application and no model call was told, so the next mail about it read as a fresh
     obligation. A thing closed on purpose is not the same as a thing never asked about,
     and only this tier can tell them apart.
  3. **The semester** — the course codes the ledger already holds, so "the CHM 113
     recitation" attaches to a class that exists instead of arriving as a new unnamed
     thing.
  4. **The people around the owner** — the entities with recent evidence in the ledger:
     who they are, what they are to the owner, when last touched. The knowledge base
     about people the goal asks for is the `entity`/`touchpoint` tables; this is their
     report, not a new store.
  5. **Short-term memory** — what is happening right now, as a deterministic report over
     the ledger: overdue and due-soon commitments, the next few plans, what is waiting on
     the owner. No model writes this; it is derived fresh from typed records every time,
     so it can never drift from the ledger it summarizes.

Three properties, inherited from `owner_context` and load-bearing for the same reasons:

  - **Deterministic.** Fixed ordering everywhere (dates then ids), so the same ledger
    renders the same block and a caching backend is not defeated by iteration order.
  - **Empty when there is nothing.** A section with no data contributes nothing, and a
    fresh install produces "" — byte-for-byte the prompt it sent before this existed.
  - **Ledger-only.** Facts, entities, commitments, engagements, questions. No similarity
    scores, no embeddings — retrieval is additive by CLAUDE.md's ruling, and a context
    block that needed a vector store would be the additive layer turning load-bearing.

Facts are stated, never instructions: the block says what is true, and each prompt's own
rules say what to do about it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
from typing import Any

from backglass.config import Settings
from backglass.ledger import USER_ID

#: Ceiling for the people section. Same shape as facts.OWNER_CONTEXT_CHARS and there
#: for the same reason: cost tracks output, not input (2026-08-07 audit), so a few
#: hundred characters are nearly free today — the cap is for the day that changes.
PEOPLE_CHARS = 700

#: Ceiling for the short-term section.
NOW_CHARS = 900

#: How many people the section may name. The goal says "main people"; a ledger that
#: has touched two hundred entities still only has a handful the owner is actively
#: entangled with, and a roster is noise where a cast list is context.
PEOPLE_LIMIT = 8

#: Evidence window for "recent" — a person with nothing in the ledger for this many
#: days is not part of the current picture, whatever their history.
PEOPLE_WINDOW_DAYS = 90

#: How many commitments / plans the situation section may spell out. Counts carry the
#: rest.
LINE_LIMIT = 5

#: Due-soon horizon, matching the planner's near week.
DUE_SOON_DAYS = 7

#: Ceiling for the settled section. Larger than the others on purpose: a decision is the
#: one kind of context whose absence causes the model to re-open something the owner has
#: closed, and re-deciding a settled question is the expensive error here.
DECIDED_CHARS = 900

#: Ceiling for the semester section. Six courses with instructors fit comfortably; the cap
#: exists for a ledger that has accumulated several terms.
SEMESTER_CHARS = 600

#: How many standing decisions the section may spell out, newest first. A decision the
#: owner made a year ago is still standing, but it is not what the next message is about.
DECISION_LIMIT = 8

#: A course code, as `courses.py` matches it — two to four letters and three digits, on a
#: word boundary so the `2026` in a term prefix cannot be read as a course number.
_COURSE_CODE = re.compile(r"\b([A-Z]{2,4})\s?-?\s?(\d{3})\b")


def assemble(
    conn: sqlite3.Connection, settings: Settings, *, day: date | None = None
) -> str:
    """The three tiers as one block a prompt can carry, or "" when the ledger is empty.

    `day` is the local today; tests pin it, production reads the active-timezone clock
    (the same clock the planner runs on, so "overdue" means the same thing in both).
    """
    from backglass import facts
    from backglass.plan import timezones

    if day is None:
        day = timezones.local_now(settings).date()

    sections = [
        facts.owner_context(conn),
        _decided(conn),
        _semester(conn),
        _people(conn, day),
        _situation(conn, day),
    ]
    return "\n\n".join(s for s in sections if s)


def _decided(conn: sqlite3.Connection, *, limit: int = DECIDED_CHARS) -> str:
    """What the owner has already settled, so nothing re-opens it.

    The gap this closes, 2026-08-23: the ledger knew "Sallie Mae application: not doing
    it" and no model call was told. So a later mail about the same application read as a
    fresh obligation, because from the reader's side it was one — the decision lived in a
    table nobody showed it. Facts say who the owner is and the situation says what is
    open; neither says what has been closed on purpose, and that is a different thing
    from something that was never asked about.

    Stated, never instructed, exactly like the tiers around it. This block says a decision
    exists and what it was; whether a message is nonetheless a live obligation is the
    prompt's judgement, and a decision is evidence for that judgement rather than a veto
    over it. The owner changes their mind, and `decisions.revisit` is how that is recorded
    — a model told "never extract this again" would make that unrecoverable.
    """
    from backglass import decisions
    from backglass.questions import settles_an_obligation

    rows = [d for d in decisions.active(conn) if settles_an_obligation(d)][:DECISION_LIMIT]
    if not rows:
        return ""
    lines: list[str] = []
    used = 0
    for decision in rows:
        line = f"- {_clip(decision.title, at=60)}: {_clip(decision.choice, at=70)}"
        if decision.reasoning:
            line += f" ({_clip(decision.reasoning, at=60)})"
        line += f" — decided {decision.decided_at[:10]}"
        if used + len(line) + 1 > limit:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return (
        "WHAT THE OWNER HAS ALREADY DECIDED (settled on purpose; not open questions)\n"
        + "\n".join(lines)
    )


def _semester(conn: sqlite3.Connection, *, limit: int = SEMESTER_CHARS) -> str:
    """The classes the owner is in, so a course code is recognised rather than invented.

    Read with a direct query rather than through `courses.load`, for two reasons. That
    function orders by next meeting, which shifts hour to hour and would give a different
    block every run — the determinism this module states as load-bearing exists so a
    caching backend is not defeated, and an ordering that tracks the clock defeats it by
    construction. And it assembles meetings, key dates, obligations and documents per
    course, none of which a reader of one message needs.

    `courses.py` owns the rich reading of what a course is, including its components and
    the three spellings of them; this is the cheap one — the codes, sorted. Both read the
    same rows, and a course that appears here and not there means the calendar has an
    event whose title carries a code and whose payload does not parse, which is worth
    finding out rather than papering over.

    What it buys: "the CHM 113 recitation activity" attaches to a course the ledger
    already knows instead of arriving as a new unnamed thing, and a mail about a class the
    owner is not in reads as what it is.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT si.title
        FROM source_item si
        WHERE si.user_id = :user_id AND si.source = 'calendar:asu'
          AND NOT EXISTS (SELECT 1 FROM source_item_retraction r
                          WHERE r.source_item_id = si.id)
        ORDER BY si.title
        """,
        {"user_id": USER_ID},
    ).fetchall()
    subjects = sorted({code for row in rows if (code := _course_code(row["title"]))})
    if not subjects:
        return ""
    lines: list[str] = []
    used = 0
    for subject in subjects:
        line = f"- {subject}"
        if used + len(line) + 1 > limit:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return (
        "THE OWNER'S CURRENT CLASSES (course codes the ledger already knows)\n"
        + "\n".join(lines)
    )


def _course_code(text: object) -> str | None:
    """The course code in a class calendar title, or None.

    The same shape `courses.py` matches on, applied only to `calendar:asu` rows — a source
    that holds nothing but class meetings. Run over arbitrary text it would pull a room
    number out of "Armstrong Hall 147"; bounded to that one source, a three-digit number
    after a short uppercase word is a course and nothing else.
    """
    match = _COURSE_CODE.search(" ".join(str(text or "").split()).upper())
    return f"{match.group(1)} {match.group(2)}" if match else None


def _people(conn: sqlite3.Connection, day: date, *, limit: int = PEOPLE_CHARS) -> str:
    """The entities with recent evidence, most-touched first.

    Evidence is anything in the ledger that names the person: a commitment they are
    counterparty to, a plan they are part of, a touchpoint the owner recorded. The
    threshold comparison is on `substr(…, 1, 10)` — occurred_at keeps each source's own
    offset and date() walks evening Phoenix rows into the next day (lessons, 2026-08-01).
    """
    floor = (day - timedelta(days=PEOPLE_WINDOW_DAYS)).isoformat()
    rows = conn.execute(
        """
        SELECT e.canonical_name, e.role, e.org, e.tags_json,
               COUNT(*) AS evidence,
               MAX(substr(ev.occurred_at, 1, 10)) AS last_seen
        FROM entity e
        JOIN (
          SELECT c.counterparty_entity_id AS entity_id, si.occurred_at AS occurred_at
            FROM commitment c JOIN source_item si ON si.id = c.source_item_id
           WHERE c.user_id = :user_id AND c.counterparty_entity_id IS NOT NULL
          UNION ALL
          SELECT ep.entity_id, si.occurred_at
            FROM engagement_person ep
            JOIN engagement en ON en.id = ep.engagement_id
            JOIN source_item si ON si.id = en.source_item_id
           WHERE ep.user_id = :user_id
          UNION ALL
          SELECT t.entity_id, t.occurred_at
            FROM touchpoint t
           WHERE t.user_id = :user_id
        ) ev ON ev.entity_id = e.id
        WHERE e.user_id = :user_id
          AND e.kind = 'person'
          AND substr(ev.occurred_at, 1, 10) >= :floor
        GROUP BY e.id
        ORDER BY evidence DESC, e.canonical_name ASC, e.id ASC
        LIMIT :limit
        """,
        {"user_id": USER_ID, "floor": floor, "limit": PEOPLE_LIMIT},
    ).fetchall()
    if not rows:
        return ""

    lines: list[str] = []
    used = 0
    for r in rows:
        line = f"- {r['canonical_name']}{_person_detail(r)}"
        if used + len(line) + 1 > limit:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return "PEOPLE AROUND THE OWNER (from the ledger's evidence, most active first)\n" + \
        "\n".join(lines)


def _person_detail(row: sqlite3.Row | dict[str, Any]) -> str:
    """" — role, org (tags; last seen YYYY-MM-DD)" from whatever the profile has."""
    who = ", ".join(p for p in (row["role"], row["org"]) if p)
    try:
        tags = [str(t) for t in json.loads(row["tags_json"] or "[]")]
    except (TypeError, ValueError):
        tags = []
    inside = "; ".join(
        p for p in (", ".join(tags), f"last seen {row['last_seen']}") if p
    )
    out = f" — {who}" if who else ""
    return f"{out} ({inside})" if inside else out


def _situation(conn: sqlite3.Connection, day: date, *, limit: int = NOW_CHARS) -> str:
    """Short-term memory: the ledger's open state, as counts plus the nearest items.

    Every date comparison here is on `substr(col, 1, 10)`, for the reason
    brief_engagements.sql documents: `due_at` and `starts_at` mix bare dates, naive
    locals and offset-bearing datetimes, and only the leading ten characters mean the
    local day under all three shapes — date() would walk an evening Phoenix row into
    the next day (lessons, 2026-08-01).
    """
    today = day.isoformat()
    horizon = (day + timedelta(days=DUE_SOON_DAYS)).isoformat()

    totals = conn.execute(
        """
        SELECT COUNT(*) AS open_count,
               SUM(CASE WHEN due_at IS NOT NULL AND substr(due_at, 1, 10) < :today
                        THEN 1 ELSE 0 END) AS overdue,
               SUM(CASE WHEN substr(due_at, 1, 10) BETWEEN :today AND :horizon
                        THEN 1 ELSE 0 END) AS due_soon
        FROM commitment
        WHERE user_id = :user_id AND status = 'open'
        """,
        {"user_id": USER_ID, "today": today, "horizon": horizon},
    ).fetchone()
    if not totals or not totals["open_count"]:
        head_lines: list[str] = []
    else:
        head_lines = [
            f"- {totals['open_count']} open commitments: "
            f"{totals['overdue'] or 0} overdue, "
            f"{totals['due_soon'] or 0} due within {DUE_SOON_DAYS} days"
        ]

    # Soonest-due first, then the most *recently* lapsed — not the oldest.
    #
    # This was `due_at <= horizon ORDER BY due_at ASC` until 2026-08-24, which sounds
    # right and is exactly backwards: the overdue set only ever grows at its old end, so
    # the five lines every model call reads as "the owner's current situation" were the
    # five most-lapsed rows on the board, permanently. Measured on the live ledger that
    # morning, with 37 things due inside the week, the block named five obligations from
    # March and April and not one of them. Every triage, every extraction and every
    # relevance verdict was reading that (pipeline-audit-2026-08-21 §4a).
    #
    # A thing that lapsed yesterday is live and worth naming. A thing that lapsed in March
    # is either dead or already in the staleness queue being asked about, and either way it
    # is not what the next message is about. The counts above still cover everything, so
    # nothing is hidden by the reordering — 53 overdue is still 53 overdue.
    nearest = conn.execute(
        """
        SELECT c.what, substr(c.due_at, 1, 10) AS due_day, c.direction, e.canonical_name
        FROM commitment c
        LEFT JOIN entity e ON e.id = c.counterparty_entity_id
        WHERE c.user_id = :user_id AND c.status = 'open'
          AND c.due_at IS NOT NULL AND substr(c.due_at, 1, 10) <= :horizon
        ORDER BY
          -- Due-soon (today onward) before anything already lapsed.
          CASE WHEN substr(c.due_at, 1, 10) >= :today THEN 0 ELSE 1 END,
          -- Within that: soonest first for what is coming, newest lapse first for what
          -- is gone. Two directions on purpose — the nearest edge of "now" in both cases.
          CASE WHEN substr(c.due_at, 1, 10) >= :today
               THEN substr(c.due_at, 1, 10) END ASC,
          CASE WHEN substr(c.due_at, 1, 10) <  :today
               THEN substr(c.due_at, 1, 10) END DESC,
          c.id ASC
        LIMIT :limit
        """,
        {"user_id": USER_ID, "today": today, "horizon": horizon, "limit": LINE_LIMIT},
    ).fetchall()
    for r in nearest:
        owed = "they owe the owner" if r["direction"] == "owed_to_me" else "the owner owes"
        who = f" {r['canonical_name']}" if r["canonical_name"] else ""
        flag = " OVERDUE" if r["due_day"] < today else ""
        head_lines.append(
            f'- {owed}{who}: "{_clip(r["what"])}" (due {r["due_day"]}{flag})'
        )

    plans = conn.execute(
        """
        SELECT what, substr(starts_at, 1, 10) AS on_day
        FROM engagement
        WHERE user_id = :user_id AND status IN ('proposed', 'confirmed')
          AND starts_at IS NOT NULL
          AND substr(starts_at, 1, 10) BETWEEN :today AND :horizon
        ORDER BY substr(starts_at, 1, 10) ASC, id ASC
        LIMIT :limit
        """,
        {"user_id": USER_ID, "today": today, "horizon": horizon, "limit": LINE_LIMIT},
    ).fetchall()
    for r in plans:
        head_lines.append(f'- plan: "{_clip(r["what"])}" on {r["on_day"]}')

    waiting = conn.execute(
        "SELECT COUNT(*) AS n FROM open_question WHERE user_id = ? AND status = 'open'",
        (USER_ID,),
    ).fetchone()
    if waiting and waiting["n"]:
        head_lines.append(f"- {waiting['n']} question(s) waiting on the owner's answer")

    if not head_lines:
        return ""

    lines: list[str] = []
    used = 0
    for line in head_lines:
        if used + len(line) + 1 > limit:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return f"THE OWNER'S CURRENT SITUATION (as of {today}, from the ledger)\n" + \
        "\n".join(lines)


def _clip(text: str, *, at: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= at else text[: at - 1] + "…"
