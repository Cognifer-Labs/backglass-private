"""Turn a preset into rows: one goal, one target per step and per cadence, one roadmap.

All of it in a single transaction. A half-instantiated roadmap — a goal with three of its
seven steps — is worse than no roadmap, because the goal engine would immediately start
computing progress and risk against a plan that does not exist.

`Adjustments` is the interview's output applied on the way in. It is a plain dataclass
rather than model JSON so that the same structure can be hand-built by the dashboard
later; `apply_adjustments` is public for exactly that reuse.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID
from backglass.roadmap.presets import Preset

STEP_ACTIONS = ("keep", "skip", "redate")


@dataclass(frozen=True)
class StepAction:
    step_key: str
    action: str  # keep|skip|redate
    planned_date: date | None = None


@dataclass(frozen=True)
class AddedStep:
    key: str
    title: str
    planned_date: date | None = None
    after_step_key: str | None = None


@dataclass(frozen=True)
class CadenceCount:
    cadence_key: str
    weekly_count: int


@dataclass
class Adjustments:
    step_actions: list[StepAction] = field(default_factory=list)
    added_steps: list[AddedStep] = field(default_factory=list)
    cadence_counts: list[CadenceCount] = field(default_factory=list)
    summary: str = ""


def instantiate(
    conn: sqlite3.Connection,
    settings: Settings,
    preset: Preset,
    start_date: date,
    adjustments: Adjustments | None = None,
) -> int:
    """Create the goal, its targets, and the roadmap. Returns the roadmap id.

    `settings` is taken but unused today; every other entry point in the goal engine
    carries it, and threading it now keeps the call sites uniform when week-start or
    working-day logic starts shaping planned dates.
    """
    del settings

    conn.execute("BEGIN")
    try:
        goal_id = _insert_goal(conn, preset, start_date)
        roadmap_id = _insert_roadmap(
            conn, preset, goal_id, personalized=adjustments is not None
        )
        _insert_cadences(conn, preset, roadmap_id, goal_id)
        _insert_totals(conn, preset, goal_id)
        _insert_steps(conn, preset, roadmap_id, goal_id, start_date)
        if adjustments is not None:
            apply_adjustments(conn, roadmap_id, adjustments)
        recompute_target_date(conn, roadmap_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return roadmap_id


def _insert_goal(conn: sqlite3.Connection, preset: Preset, start_date: date) -> int:
    latest = max((_planned(start_date, s.offset_weeks) for s in preset.steps), default=None)
    conn.execute(
        "INSERT INTO goal (user_id, title, horizon, target_date, definition_of_done, "
        " status, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?)",
        (
            USER_ID,
            preset.title,
            preset.horizon,
            latest.isoformat() if latest else None,
            preset.definition_of_done,
            now_iso(),
        ),
    )
    return _last_id(conn)


def _insert_roadmap(
    conn: sqlite3.Connection, preset: Preset, goal_id: int, *, personalized: bool
) -> int:
    conn.execute(
        "INSERT INTO roadmap (user_id, path_id, path_version, title, goal_id, status, "
        " personalized, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
        (
            USER_ID,
            preset.id,
            preset.version,
            preset.title,
            goal_id,
            int(personalized),
            now_iso(),
        ),
    )
    return _last_id(conn)


def _insert_cadences(
    conn: sqlite3.Connection, preset: Preset, roadmap_id: int, goal_id: int
) -> None:
    for cadence in preset.cadences:
        conn.execute(
            "INSERT INTO target (goal_id, kind, title, weekly_count, estimated_minutes_each, "
            " active, created_at) VALUES (?, 'cadence', ?, ?, ?, 1, ?)",
            (
                goal_id,
                cadence.title,
                cadence.weekly_count,
                cadence.estimated_minutes_each,
                now_iso(),
            ),
        )
        conn.execute(
            "INSERT INTO roadmap_cadence (roadmap_id, cadence_key, target_id) VALUES (?, ?, ?)",
            (roadmap_id, cadence.key, _last_id(conn)),
        )


def _insert_totals(conn: sqlite3.Connection, preset: Preset, goal_id: int) -> None:
    """Totals hang off the goal directly — no link table. The roadmap page finds
    them through goal_id, and they survive step edits untouched."""
    for total in preset.totals:
        add_total(conn, goal_id, total.title, total.total_count)


def add_total(conn: sqlite3.Connection, goal_id: int, title: str, total_count: int) -> int:
    """Public: the CLI and any goal can attach a lifetime accumulator (Phase 10)."""
    if total_count <= 0:
        raise ValueError("total_count must be positive")
    conn.execute(
        "INSERT INTO target (goal_id, kind, title, total_count, active, created_at) "
        "VALUES (?, 'total', ?, ?, 1, ?)",
        (goal_id, title, total_count, now_iso()),
    )
    return _last_id(conn)


def _insert_steps(
    conn: sqlite3.Connection, preset: Preset, roadmap_id: int, goal_id: int, start_date: date
) -> None:
    # File order, not date order: a preset may deliberately run two tracks in parallel.
    for index, step in enumerate(preset.steps):
        target_id = _insert_milestone(conn, goal_id, step.title)
        conn.execute(
            "INSERT INTO roadmap_step (roadmap_id, step_key, title, detail, sort_order, "
            " planned_date, status, target_id, origin) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, 'preset')",
            (
                roadmap_id,
                step.key,
                step.title,
                step.detail,
                index * 10,  # gaps so an added step slots in without renumbering
                _planned(start_date, step.offset_weeks).isoformat(),
                target_id,
            ),
        )


def _insert_milestone(conn: sqlite3.Connection, goal_id: int, title: str) -> int:
    conn.execute(
        "INSERT INTO target (goal_id, kind, title, active, created_at) "
        "VALUES (?, 'milestone', ?, 1, ?)",
        (goal_id, title, now_iso()),
    )
    return _last_id(conn)


# ───────────────────────────────────────────────────────────── adjustments


def apply_adjustments(
    conn: sqlite3.Connection, roadmap_id: int, adjustments: Adjustments
) -> list[str]:
    """Apply the interview's edits. Returns a note per item that could not be applied.

    Unknown keys are reported rather than raised: the interview is a model output, and one
    hallucinated step key should not throw away the six good edits beside it. The notes are
    what the Sources panel shows, in the spirit of CLAUDE.md rule 5.
    """
    ignored: list[str] = []

    for action in adjustments.step_actions:
        if action.action not in STEP_ACTIONS:
            ignored.append(f"unknown step action {action.action!r} for {action.step_key!r}")
            continue
        step = _step_by_key(conn, roadmap_id, action.step_key)
        if step is None:
            ignored.append(f"unknown step key {action.step_key!r}")
            continue
        if action.action == "skip":
            _set_skipped(conn, int(step["id"]), step["target_id"], skipped=True)
        elif action.action == "redate":
            if action.planned_date is None:
                ignored.append(f"redate of {action.step_key!r} carried no date")
                continue
            conn.execute(
                "UPDATE roadmap_step SET planned_date = ? WHERE id = ?",
                (action.planned_date.isoformat(), step["id"]),
            )

    for added in adjustments.added_steps:
        _add_step(conn, roadmap_id, added, ignored)

    for count in adjustments.cadence_counts:
        row = conn.execute(
            "SELECT target_id FROM roadmap_cadence WHERE roadmap_id = ? AND cadence_key = ?",
            (roadmap_id, count.cadence_key),
        ).fetchone()
        if row is None:
            ignored.append(f"unknown cadence key {count.cadence_key!r}")
            continue
        conn.execute(
            "UPDATE target SET weekly_count = ? WHERE id = ?",
            (count.weekly_count, row["target_id"]),
        )

    recompute_target_date(conn, roadmap_id)
    return ignored


def _add_step(
    conn: sqlite3.Connection, roadmap_id: int, added: AddedStep, ignored: list[str]
) -> None:
    step_key = f"interview:{added.key}"
    if _step_by_key(conn, roadmap_id, step_key) is not None:
        ignored.append(f"step {step_key!r} already exists")
        return

    anchor = None
    if added.after_step_key is not None:
        anchor = _step_by_key(conn, roadmap_id, added.after_step_key)
        if anchor is None:
            # The anchor is placement, not identity — losing the step over a bad hint
            # would discard content the owner asked for. Append and say so.
            ignored.append(
                f"step {added.key!r} anchored after unknown {added.after_step_key!r}; appended"
            )
    base = int(anchor["sort_order"]) if anchor else _max_sort_order(conn, roadmap_id)
    conn.execute(
        "UPDATE roadmap_step SET sort_order = sort_order + 10 "
        "WHERE roadmap_id = ? AND sort_order > ?",
        (roadmap_id, base),
    )

    goal_id = _goal_id(conn, roadmap_id)
    target_id = _insert_milestone(conn, goal_id, added.title)
    conn.execute(
        "INSERT INTO roadmap_step (roadmap_id, step_key, title, sort_order, planned_date, "
        " status, target_id, origin) VALUES (?, ?, ?, ?, ?, 'pending', ?, 'interview')",
        (
            roadmap_id,
            step_key,
            added.title,
            base + 10,
            added.planned_date.isoformat() if added.planned_date else None,
            target_id,
        ),
    )


# ──────────────────────────────────────────────────────────────── helpers


def recompute_target_date(conn: sqlite3.Connection, roadmap_id: int) -> None:
    """The goal is done when its last *live* step is done, so skipped steps do not count.

    Leaving a skipped final step in the maximum would hold the target date out past
    anything the owner still intends to do, and `goals.health.risk` would then read the
    roadmap as comfortably on track.
    """
    row = conn.execute(
        "SELECT MAX(planned_date) AS latest FROM roadmap_step "
        "WHERE roadmap_id = ? AND status != 'skipped'",
        (roadmap_id,),
    ).fetchone()
    conn.execute(
        "UPDATE goal SET target_date = ? WHERE id = ?",
        (row["latest"] if row else None, _goal_id(conn, roadmap_id)),
    )


def _set_skipped(
    conn: sqlite3.Connection, step_id: int, target_id: Any, *, skipped: bool
) -> None:
    conn.execute(
        "UPDATE roadmap_step SET status = ?, done_at = NULL WHERE id = ?",
        ("skipped" if skipped else "pending", step_id),
    )
    if target_id is not None:
        conn.execute(
            "UPDATE target SET active = ? WHERE id = ?", (0 if skipped else 1, target_id)
        )


def _step_by_key(
    conn: sqlite3.Connection, roadmap_id: int, step_key: str
) -> dict[str, Any] | None:
    row: dict[str, Any] | None = conn.execute(
        "SELECT * FROM roadmap_step WHERE roadmap_id = ? AND step_key = ?",
        (roadmap_id, step_key),
    ).fetchone()
    return row


def _max_sort_order(conn: sqlite3.Connection, roadmap_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -10) AS n FROM roadmap_step WHERE roadmap_id = ?",
        (roadmap_id,),
    ).fetchone()
    return int(row["n"])


def _goal_id(conn: sqlite3.Connection, roadmap_id: int) -> int:
    row = conn.execute("SELECT goal_id FROM roadmap WHERE id = ?", (roadmap_id,)).fetchone()
    if row is None:
        raise ValueError(f"no roadmap {roadmap_id}")
    return int(row["goal_id"])


def _planned(start_date: date, offset_weeks: int) -> date:
    return start_date + timedelta(weeks=offset_weeks)


def _last_id(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
