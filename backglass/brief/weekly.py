"""Monday planning and the Friday retrospective. docs/04 §2.7.

  W1  Monday planning **replaces** the brief; it does not arrive in addition to it.
      "Two emails on Monday means neither is read."
  W2  Friday retro appends and never exceeds 150 words.
  W3  Both are generated from the ledger with no manual input required. "Input is optional
      enrichment, never a precondition."

W1 is enforced in `brief.daily.build_for` rather than by discipline: on a week-start day
the daily sections are not built at all, so there is no path that produces two.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from backglass.brief.model import Brief, LedgerRef, Line, Note, Section
from backglass.config import Settings
from backglass.goals import checkpoints as checkpoints_mod
from backglass.goals import health
from backglass.goals import targets as targets_mod
from backglass.ledger import USER_ID
from backglass.plan import estimates

#: W2. The retro is an appendix, not a second brief.
FRIDAY_WORD_LIMIT = 150

#: docs/04 §2.7 Monday: "Commitments aging past 14 days with no movement."
AGING_DAYS = 14


def is_week_start(settings: Settings, day: date) -> bool:
    names = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    return names[day.weekday()] == settings.week_start.lower()


def is_week_end(settings: Settings, day: date) -> bool:
    """The last working day of the week — Friday when the week starts Monday."""
    start_index = (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ).index(settings.week_start.lower())
    return day.weekday() == (start_index + 4) % 7


# ── Monday planning ───────────────────────────────────────────────────────


def monday(conn: sqlite3.Connection, settings: Settings, day: date) -> Brief:
    """W1. Replaces the brief. Generated entirely from the ledger (W3)."""
    brief = Brief(generated_for_date=day.isoformat(), kind="monday")
    week_start = targets_mod.week_start_of(day, settings.week_start)
    last_week = week_start - timedelta(days=7)

    # ── last week's targets, hit and missed, with counts
    scored = Section(priority=1, title="Last week")
    for target in targets_mod.progress(conn, settings, last_week + timedelta(days=1)):
        mark = "hit" if target.complete else "missed"
        label = f"{target.goal_title} — {target.title}"
        count = (
            f"{target.done_this_week}/{target.weekly_count}"
            if target.weekly_count
            else str(target.done_this_week)
        )
        scored.lines.append(
            Line(
                text=f"{label}: {count} {mark}.",
                provenance=LedgerRef(
                    "goals", str(target.goal_id), f"goal · {target.goal_title}"
                ),
                status=None if target.complete else "slipping",
            )
        )
    brief.add(scored)

    # ── capacity check for the coming week (§2.3, G5–G7)
    check = targets_mod.capacity_check(conn, settings, day)
    sentence = check.sentence(settings)
    if sentence:
        capacity = Section(priority=2, title="Capacity")
        capacity.lines.append(
            Line(
                text=sentence,
                provenance=LedgerRef("plans", week_start.isoformat(), f"week of {week_start}"),
                status="overdue" if check.over else None,
            )
        )
        if check.over:
            # G6. List targets by cost, largest first. Do not auto-drop anything.
            for target in check.by_cost:
                if not target.weekly_minutes:
                    continue
                capacity.lines.append(
                    Line(
                        text=(
                            f"{target.title}: {target.weekly_minutes / 60:.1f}h/wk "
                            f"({target.weekly_count} × {target.estimated_minutes_each}m)"
                        ),
                        provenance=LedgerRef(
                            "goals", str(target.goal_id), f"goal · {target.goal_title}"
                        ),
                    )
                )
        brief.add(capacity)

    # ── goals at risk, with the G14 question
    risky = Section(priority=3, title="At risk")
    for goal in health.sustained_risk(conn, settings, day):
        risky.lines.append(
            Line(
                text=health.question_for(goal),
                provenance=LedgerRef("goals", str(goal.goal_id), f"goal · {goal.goal_title}"),
                status="overdue",
            )
        )
    for goal in health.risk(conn, settings, day):
        if goal.at_risk:
            line = goal.sentence()
            if line:
                risky.lines.append(
                    Line(
                        text=line,
                        provenance=LedgerRef(
                            "goals", str(goal.goal_id), f"goal · {goal.goal_title}"
                        ),
                        status="slipping",
                    )
                )
    brief.add(risky)

    # ── targets flagged unrealistic under G4
    unrealistic = Section(priority=4, title="Unrealistic")
    for target in health.unrealistic_targets(conn, settings, day):
        unrealistic.lines.append(
            Line(
                # G4's framing: lowering a target is a legitimate outcome, so this is a
                # question about the target and not a report on the person.
                text=(
                    f"{target.title} missed {target.missed_weeks} weeks running "
                    f"at {target.weekly_count}/wk. Lower it?"
                ),
                provenance=LedgerRef(
                    "goals", str(target.goal_id), f"goal · {target.goal_title}"
                ),
                status="needs_review",
            )
        )
    brief.add(unrealistic)

    # ── G1: a goal with no target is inert, and Monday says so
    inert = Section(priority=5, title="Inert goals")
    for goal_row in targets_mod.goals_without_targets(conn):
        goal_title = str(goal_row["title"])
        inert.lines.append(
            Line(
                text=f"{goal_title}: no target. A goal without a target is inert.",
                provenance=LedgerRef("goals", str(goal_row["id"]), f"goal · {goal_title}"),
                status="needs_review",
            )
        )
    brief.add(inert)

    # ── commitments aging past 14 days with no movement
    aging = Section(priority=6, title="Aging")
    rows = conn.execute(
        "SELECT c.id, c.what, c.rollover_count, e.canonical_name AS counterparty, "
        "       s.source, s.external_id, s.occurred_at, s.title "
        "FROM commitment c JOIN source_item s ON s.id = c.source_item_id "
        "LEFT JOIN entity e ON e.id = c.counterparty_entity_id "
        "WHERE c.user_id = ? AND c.status = 'open' AND c.confidence >= ? "
        "  AND julianday(date(?)) - julianday(date(s.occurred_at)) >= ? "
        "ORDER BY s.occurred_at",
        (USER_ID, settings.confidence_threshold, day.isoformat(), AGING_DAYS),
    ).fetchall()
    for row in rows:
        age = (day - date.fromisoformat(str(row["occurred_at"])[:10])).days
        aging.lines.append(
            Line(
                text=f"{row['what']} — {age}d, no movement.",
                provenance=_source_ref(row),
                status="slipping",
            )
        )
    brief.add(aging)

    # ── last Friday's one-line answer, shown in Monday's planning (§2.7)
    note = conn.execute(
        "SELECT local_date, learned FROM shutdown_note "
        "WHERE user_id = ? AND learned IS NOT NULL AND date(local_date) >= date(?) "
        "ORDER BY local_date DESC LIMIT 1",
        (USER_ID, last_week.isoformat()),
    ).fetchone()
    if note:
        carried = Section(priority=7, title="From last week")
        carried.lines.append(
            Line(
                text=str(note["learned"]),
                provenance=LedgerRef(
                    "plans", str(note["local_date"]), f"shutdown · {note['local_date']}"
                ),
            )
        )
        brief.add(carried)

    brief.deduplicate()
    brief.enforce_word_limit()
    brief.assert_provenance()
    if not brief.sections:
        brief.notes.append(
            Note("No goals, targets or aging commitments. Nothing to plan around.")
        )
    return brief


# ── Friday retrospective ──────────────────────────────────────────────────


def friday(conn: sqlite3.Connection, settings: Settings, day: date) -> Section:
    """W2. Appended to the normal brief, never more than 150 words.

    Returned as a Section rather than a Brief because it appends — the daily brief owns
    the word budget and this is a part of it.
    """
    section = Section(priority=9, title="This week")
    week_start = targets_mod.week_start_of(day, settings.week_start)

    # ── what completed this week, grouped by goal
    rows = conn.execute(
        "SELECT COALESCE(g.title, 'No goal') AS goal_title, COUNT(*) AS n "
        "FROM commitment c LEFT JOIN goal g ON g.id = c.goal_id "
        "WHERE c.user_id = ? AND c.status = 'done' AND date(c.resolved_at) >= date(?) "
        "GROUP BY goal_title ORDER BY n DESC",
        (USER_ID, week_start.isoformat()),
    ).fetchall()
    for row in rows:
        section.lines.append(
            Line(
                text=f"{row['goal_title']}: {row['n']} completed.",
                provenance=LedgerRef("plans", week_start.isoformat(), f"week of {week_start}"),
                status="done",
            )
        )

    # ── estimate versus actual, one line
    ratio = estimates.ratio_report(conn).sentence()
    if ratio:
        section.lines.append(
            Line(
                text=ratio,
                provenance=LedgerRef("plans", week_start.isoformat(), f"week of {week_start}"),
            )
        )

    # ── anything that rolled over three or more times
    # Confidence floor, same as every other brief query (CLAUDE.md rule 2): "X rolled 4x"
    # is a claim about a commitment the owner made, and an unconfirmed extraction has no
    # business making it. It stays in the review queue until accepted.
    rolled = conn.execute(
        "SELECT c.id, c.what, c.rollover_count, "
        "       s.source, s.external_id, s.occurred_at, s.title "
        "FROM commitment c JOIN source_item s ON s.id = c.source_item_id "
        "WHERE c.user_id = ? AND c.status = 'open' AND c.rollover_count >= ? "
        "  AND c.confidence >= ? "
        "ORDER BY c.rollover_count DESC",
        (USER_ID, settings.rollover_question_at, settings.confidence_threshold),
    ).fetchall()
    for row in rolled:
        section.lines.append(
            Line(
                text=f"{row['what']} rolled {row['rollover_count']}x.",
                provenance=_source_ref(row),
                status="slipping",
            )
        )

    # ── the one prompt (§2.7). Optional, per W3 — never a precondition.
    section.notes.append(
        Note("What is the single thing that would make next week better? Reply to record it.")
    )

    _trim_to(section, FRIDAY_WORD_LIMIT)
    return section


def _trim_to(section: Section, limit: int) -> None:
    """W2. "never exceeds 150 words". Trims from the end, least important first."""
    import re

    def words() -> int:
        text = " ".join([line.text for line in section.lines] + [n.text for n in section.notes])
        return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’\-/:.]*", text))

    dropped = 0
    while words() > limit and len(section.lines) > 1:
        section.lines.pop()
        dropped += 1
    if dropped:
        section.notes.insert(0, Note(f"{dropped} more omitted for length."))


def _source_ref(row: dict[str, Any]) -> Any:
    from backglass.brief.model import SourceRef

    if row["source"]:
        return SourceRef(
            source=str(row["source"]),
            external_id=str(row["external_id"]),
            occurred_at=str(row["occurred_at"]),
            title=row["title"],
        )
    return LedgerRef("commitments", str(row["id"]), "ledger")


def link_completed_work(conn: sqlite3.Connection) -> int:
    """Turn completed goal-linked work into checkpoints. docs/04 §2.4 sources 1 and 2.

    Idempotent — `checkpoints.from_*` refuse to write a second checkpoint for the same
    block or commitment, so running this on every close is safe.
    """
    made = 0
    for row in conn.execute(
        "SELECT id FROM plan_block WHERE outcome = 'done' AND (goal_id IS NOT NULL "
        "  OR commitment_id IS NOT NULL)"
    ).fetchall():
        if checkpoints_mod.from_completed_block(conn, int(row["id"])):
            made += 1
    for row in conn.execute(
        "SELECT id FROM commitment "
        "WHERE user_id = ? AND status = 'done' AND goal_id IS NOT NULL",
        (USER_ID,),
    ).fetchall():
        if checkpoints_mod.from_resolved_commitment(conn, int(row["id"])):
            made += 1
    return made
