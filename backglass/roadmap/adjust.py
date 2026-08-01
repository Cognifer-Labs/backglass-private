"""The owner's edits to a live roadmap. One function per action, docs/06 §Write-back.

A preset is a starting position, not a contract — a roadmap the owner cannot redate, skip
or reorder becomes decoration in the same week a read-only dashboard would.

Every edit that can move a date recomputes `goal.target_date` afterwards, because the goal
engine's risk projection reads it and would otherwise be answering yesterday's question.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

from backglass.db import now_iso
from backglass.goals import checkpoints
from backglass.roadmap.instantiate import recompute_target_date


class AdjustError(RuntimeError):
    pass


@dataclass(frozen=True)
class StepResult:
    step_id: int
    status: str
    done_at: str | None
    checkpoint_id: int | None = None


def complete_step(
    conn: sqlite3.Connection, step_id: int, on: date | None = None
) -> StepResult:
    """Mark a step done and record the checkpoint that moves its target.

    Idempotent: a second call is a no-op. Two checkpoints for one milestone would double
    the goal's observed rate, and `goals.health.risk` would report a roadmap as on pace
    because the owner clicked twice.
    """
    step = _step(conn, step_id)
    if step["status"] == "done":
        return StepResult(step_id, "done", _text(step["done_at"]))

    done_at = on.isoformat() if on else now_iso()
    conn.execute(
        "UPDATE roadmap_step SET status = 'done', done_at = ? WHERE id = ?", (done_at, step_id)
    )
    checkpoint_id: int | None = None
    if step["target_id"] is not None:
        checkpoint_id = checkpoints.record(
            conn,
            int(step["target_id"]),
            source="manual",
            occurred_at=done_at,
            note=str(step["title"]),
        ).checkpoint_id
    recompute_target_date(conn, int(step["roadmap_id"]))
    return StepResult(step_id, "done", done_at, checkpoint_id)


def skip_step(conn: sqlite3.Connection, step_id: int) -> StepResult:
    """Skipped steps deactivate their target, so they leave the capacity check and the
    progress read entirely rather than sitting there as permanent unfinished work."""
    step = _step(conn, step_id)
    _set_active(conn, step, status="skipped", active=0)
    recompute_target_date(conn, int(step["roadmap_id"]))
    return StepResult(step_id, "skipped", None)


def unskip_step(conn: sqlite3.Connection, step_id: int) -> StepResult:
    step = _step(conn, step_id)
    if step["status"] != "skipped":
        raise AdjustError(f"step {step_id} is not skipped")
    _set_active(conn, step, status="pending", active=1)
    recompute_target_date(conn, int(step["roadmap_id"]))
    return StepResult(step_id, "pending", None)


def redate_step(conn: sqlite3.Connection, step_id: int, new_date: date) -> StepResult:
    step = _step(conn, step_id)
    conn.execute(
        "UPDATE roadmap_step SET planned_date = ? WHERE id = ?",
        (new_date.isoformat(), step_id),
    )
    recompute_target_date(conn, int(step["roadmap_id"]))
    return StepResult(step_id, str(step["status"]), _text(step["done_at"]))


def move_step(conn: sqlite3.Connection, step_id: int, direction: str) -> StepResult:
    """Swap sort_order with the adjacent step. Order is the owner's, not the preset's."""
    if direction not in ("up", "down"):
        raise AdjustError(f"direction must be 'up' or 'down', got {direction!r}")
    step = _step(conn, step_id)
    comparison, order = ("<", "DESC") if direction == "up" else (">", "ASC")
    neighbour = conn.execute(
        # Interpolated because a comparison operator cannot be bound; both halves come
        # from the literal tuple above, never from the caller.
        f"SELECT id, sort_order FROM roadmap_step WHERE roadmap_id = ? "
        f"AND sort_order {comparison} ? ORDER BY sort_order {order} LIMIT 1",
        (step["roadmap_id"], step["sort_order"]),
    ).fetchone()
    if neighbour is None:
        raise AdjustError(f"step {step_id} is already at the {direction} end")
    conn.execute(
        "UPDATE roadmap_step SET sort_order = ? WHERE id = ?",
        (neighbour["sort_order"], step_id),
    )
    conn.execute(
        "UPDATE roadmap_step SET sort_order = ? WHERE id = ?",
        (step["sort_order"], neighbour["id"]),
    )
    recompute_target_date(conn, int(step["roadmap_id"]))
    return StepResult(step_id, str(step["status"]), _text(step["done_at"]))


def rename_step(
    conn: sqlite3.Connection, step_id: int, title: str, detail: str | None = None
) -> StepResult:
    """The step's words are the owner's too. The linked milestone target was named
    after the step at instantiation, so its title moves with the rename — a
    checkpoint filed under a title the page no longer shows would be unreadable."""
    title = title.strip()
    if not title:
        raise AdjustError("a step needs a title")
    step = _step(conn, step_id)
    conn.execute(
        "UPDATE roadmap_step SET title = ?, detail = ? WHERE id = ?",
        (title, (detail or "").strip() or None, step_id),
    )
    if step["target_id"] is not None:
        conn.execute(
            "UPDATE target SET title = ? WHERE id = ?", (title, step["target_id"])
        )
    return StepResult(step_id, str(step["status"]), _text(step["done_at"]))


def rename_roadmap(
    conn: sqlite3.Connection,
    roadmap_id: int,
    title: str,
    definition_of_done: str | None = None,
) -> None:
    """Roadmap and goal were instantiated under one name and the goal's title is
    what risk sentences and the brief say aloud, so a rename moves both."""
    title = title.strip()
    if not title:
        raise AdjustError("a roadmap needs a title")
    row = conn.execute(
        "SELECT goal_id FROM roadmap WHERE id = ?", (roadmap_id,)
    ).fetchone()
    if row is None:
        raise AdjustError(f"no roadmap {roadmap_id}")
    conn.execute("UPDATE roadmap SET title = ? WHERE id = ?", (title, roadmap_id))
    conn.execute("UPDATE goal SET title = ? WHERE id = ?", (title, row["goal_id"]))
    if definition_of_done is not None and definition_of_done.strip():
        conn.execute(
            "UPDATE goal SET definition_of_done = ? WHERE id = ?",
            (definition_of_done.strip(), row["goal_id"]),
        )


def set_cadence(
    conn: sqlite3.Connection, roadmap_id: int, cadence_key: str, weekly_count: int
) -> None:
    """G4 in docs/04: lowering a target is a legitimate outcome, so this goes both ways."""
    row = conn.execute(
        "SELECT target_id FROM roadmap_cadence WHERE roadmap_id = ? AND cadence_key = ?",
        (roadmap_id, cadence_key),
    ).fetchone()
    if row is None:
        raise AdjustError(f"no cadence {cadence_key!r} on roadmap {roadmap_id}")
    conn.execute(
        "UPDATE target SET weekly_count = ? WHERE id = ?", (weekly_count, row["target_id"])
    )
    recompute_target_date(conn, roadmap_id)


def drop_roadmap(conn: sqlite3.Connection, roadmap_id: int) -> None:
    """Drops the goal with it. A roadmap's goal has no independent existence, and leaving
    it active would keep it in the brief and the capacity check forever."""
    row = conn.execute(
        "SELECT goal_id, status FROM roadmap WHERE id = ?", (roadmap_id,)
    ).fetchone()
    if row is None:
        raise AdjustError(f"no roadmap {roadmap_id}")
    closed = now_iso()
    conn.execute(
        "UPDATE roadmap SET status = 'dropped', closed_at = ? WHERE id = ?",
        (closed, roadmap_id),
    )
    conn.execute(
        "UPDATE goal SET status = 'dropped', closed_at = ? WHERE id = ?",
        (closed, row["goal_id"]),
    )


# ──────────────────────────────────────────────────────────────── helpers


def _step(conn: sqlite3.Connection, step_id: int) -> dict[str, Any]:
    row: dict[str, Any] | None = conn.execute(
        "SELECT * FROM roadmap_step WHERE id = ?", (step_id,)
    ).fetchone()
    if row is None:
        raise AdjustError(f"no roadmap step {step_id}")
    return row


def _set_active(
    conn: sqlite3.Connection, step: dict[str, Any], *, status: str, active: int
) -> None:
    conn.execute(
        "UPDATE roadmap_step SET status = ?, done_at = NULL WHERE id = ?", (status, step["id"])
    )
    if step["target_id"] is not None:
        conn.execute("UPDATE target SET active = ? WHERE id = ?", (active, step["target_id"]))


def _text(value: Any) -> str | None:
    return None if value is None else str(value)
