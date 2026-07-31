"""Checkpoints — the only thing that moves a target. docs/04 §2.4.

  G8   Commitments may link to at most one goal. "Multi-goal linkage sounds useful and
       makes progress uninterpretable."
  G9   A checkpoint always records what produced it, with a source link where one exists.
  G10  Deleting a checkpoint recomputes target progress immediately.

G10 is free here because progress is never stored. `targets.progress` sums checkpoints on
read, so a deletion is reflected the next time anything asks — there is no cached counter
to invalidate and therefore no way for one to go stale.

The four sources in docs/04 §2.4 are the `source` column's vocabulary: block, commitment,
manual, extraction.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from backglass.db import now_iso
from backglass.ledger import USER_ID

SOURCES = ("block", "commitment", "manual", "extraction")


class CheckpointError(ValueError):
    pass


@dataclass(frozen=True)
class Recorded:
    checkpoint_id: int
    target_id: int


def record(
    conn: sqlite3.Connection,
    target_id: int,
    *,
    source: str,
    occurred_at: str | None = None,
    source_item_id: int | None = None,
    commitment_id: int | None = None,
    note: str | None = None,
    delta: int = 1,
    activity_id: int | None = None,
) -> Recorded:
    """G9. Every checkpoint says what produced it.

    A checkpoint with `source='extraction'` and no `source_item_id` would be a claim the
    owner cannot check, which is CLAUDE.md rule 1 applied one layer down — so it is
    refused rather than stored.
    """
    if source not in SOURCES:
        raise CheckpointError(
            f"unknown checkpoint source {source!r}; expected one of {SOURCES}"
        )
    if source == "extraction" and source_item_id is None:
        raise CheckpointError("an extraction checkpoint must carry its source_item_id")
    if source == "commitment" and commitment_id is None:
        raise CheckpointError("a commitment checkpoint must carry its commitment_id")
    if conn.execute("SELECT 1 FROM target WHERE id = ?", (target_id,)).fetchone() is None:
        raise CheckpointError(f"no target {target_id}")
    if activity_id is not None and (
        conn.execute(
            "SELECT 1 FROM activity WHERE id = ? AND active = 1", (activity_id,)
        ).fetchone()
        is None
    ):
        raise CheckpointError(f"no active activity {activity_id}")

    conn.execute(
        "INSERT INTO checkpoint (target_id, occurred_at, source, source_item_id, "
        " commitment_id, note, delta, activity_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            target_id,
            occurred_at or now_iso(),
            source,
            source_item_id,
            commitment_id,
            note,
            delta,
            activity_id,
        ),
    )
    return Recorded(
        checkpoint_id=int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]),
        target_id=target_id,
    )


def link_commitment(conn: sqlite3.Connection, commitment_id: int, goal_id: int | None) -> None:
    """G8. At most one goal, enforced by the column being scalar and by this being the
    only way to set it."""
    if goal_id is not None and not _goal_exists(conn, goal_id):
        raise CheckpointError(f"no goal {goal_id}")
    conn.execute("UPDATE commitment SET goal_id = ? WHERE id = ?", (goal_id, commitment_id))


def _goal_exists(conn: sqlite3.Connection, goal_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM goal WHERE id = ? AND user_id = ?", (goal_id, USER_ID)
        ).fetchone()
        is not None
    )


def from_completed_block(conn: sqlite3.Connection, block_id: int) -> Recorded | None:
    """docs/04 §2.4 source 1: "a completed scheduled block linked to a goal"."""
    row = conn.execute(
        "SELECT b.id, b.goal_id, b.title, b.ends_at, b.commitment_id, b.outcome "
        "FROM plan_block b WHERE b.id = ?",
        (block_id,),
    ).fetchone()
    if row is None or row["outcome"] != "done":
        return None
    goal_id = row["goal_id"]
    if goal_id is None and row["commitment_id"] is not None:
        linked = conn.execute(
            "SELECT goal_id FROM commitment WHERE id = ?", (row["commitment_id"],)
        ).fetchone()
        goal_id = linked["goal_id"] if linked else None
    if goal_id is None:
        return None

    target = _default_target(conn, int(goal_id))
    if target is None:
        return None
    if conn.execute(
        "SELECT 1 FROM checkpoint WHERE target_id = ? AND source = 'block' AND note = ?",
        (target, f"block:{block_id}"),
    ).fetchone():
        return None  # idempotent: marking a block done twice is one checkpoint
    return record(
        conn,
        target,
        source="block",
        occurred_at=str(row["ends_at"]),
        commitment_id=row["commitment_id"],
        note=f"block:{block_id}",
    )


def from_resolved_commitment(conn: sqlite3.Connection, commitment_id: int) -> Recorded | None:
    """docs/04 §2.4 source 2: "a completed commitment tagged with a goal"."""
    row = conn.execute(
        "SELECT id, goal_id, status, resolved_at, source_item_id FROM commitment WHERE id = ?",
        (commitment_id,),
    ).fetchone()
    if row is None or row["status"] != "done" or row["goal_id"] is None:
        return None
    target = _default_target(conn, int(row["goal_id"]))
    if target is None:
        return None
    if conn.execute(
        "SELECT 1 FROM checkpoint "
        "WHERE target_id = ? AND commitment_id = ? AND source = 'commitment'",
        (target, commitment_id),
    ).fetchone():
        return None
    return record(
        conn,
        target,
        source="commitment",
        occurred_at=str(row["resolved_at"] or now_iso()),
        commitment_id=commitment_id,
        source_item_id=row["source_item_id"],
    )


def _default_target(conn: sqlite3.Connection, goal_id: int) -> int | None:
    """Which target a goal-linked completion advances.

    The oldest active cadence target, falling back to the oldest active target of any
    kind. A goal usually has one cadence target and this is unambiguous; where it has
    several, guessing between them would make progress uninterpretable in exactly the way
    G8 warns about, so the owner links explicitly with `record(...)` instead.
    """
    row = conn.execute(
        "SELECT id FROM target WHERE goal_id = ? AND active = 1 "
        "ORDER BY CASE kind WHEN 'cadence' THEN 0 ELSE 1 END, id LIMIT 1",
        (goal_id,),
    ).fetchone()
    return int(row["id"]) if row else None


def delete(conn: sqlite3.Connection, checkpoint_id: int) -> None:
    """G10. Progress is summed on read, so this recomputes by definition."""
    conn.execute("DELETE FROM checkpoint WHERE id = ?", (checkpoint_id,))
