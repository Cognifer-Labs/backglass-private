"""The activity registry. Phase A, docs/14 F1.

A discrete extracurricular — organization, role, supervisor, date range — that hour
checkpoints attach to, so the AMCAS Work & Activities section can later be assembled
from evidence instead of memory. Categories map to the medical preset's total keys
plus `other`; the mapping is a plain key rather than a foreign key so activities
survive a roadmap being dropped and re-instantiated.

Per-activity hours are `SUM(delta)` over the activity's checkpoints on total targets,
computed on read (G3, G10): deleting a checkpoint recomputes the activity's hours the
same way it recomputes the target's. Only total-target checkpoints count — those carry
hours; a cadence tick that happens to name an activity is a session, not an hour figure.

AMCAS allows 15 activities and 3 "most meaningful". Both are surfaced as counts and
never enforced — AMCAS enforces its own caps, and refusing a 16th row here would only
push the overflow back into a spreadsheet.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from backglass.db import now_iso
from backglass.ledger import USER_ID

CATEGORIES = ("shadowing", "clinical", "volunteering", "research", "leadership", "other")

AMCAS_SLOTS = 15
AMCAS_MOST_MEANINGFUL = 3
AMCAS_DESCRIPTION_CHARS = 700
AMCAS_MEANINGFUL_CHARS = 1325


class ActivityError(ValueError):
    pass


def add(
    conn: sqlite3.Connection,
    *,
    title: str,
    org: str | None = None,
    role: str | None = None,
    category: str = "other",
    contact_entity_id: int | None = None,
    started_on: str | None = None,
) -> int:
    title = title.strip()
    if not title:
        raise ActivityError("an activity needs a title")
    if category not in CATEGORIES:
        raise ActivityError(
            f"unknown category {category!r}; expected one of {CATEGORIES}"
        )
    conn.execute(
        "INSERT INTO activity (user_id, title, org, role, category, contact_entity_id, "
        " started_on, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            USER_ID,
            title,
            (org or "").strip() or None,
            (role or "").strip() or None,
            category,
            contact_entity_id,
            started_on,
            now_iso(),
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def toggle_most_meaningful(conn: sqlite3.Connection, activity_id: int) -> bool:
    row = _get(conn, activity_id)
    flag = 0 if row["most_meaningful"] else 1
    conn.execute("UPDATE activity SET most_meaningful = ? WHERE id = ?", (flag, activity_id))
    return bool(flag)


def end(conn: sqlite3.Connection, activity_id: int, ended_on: str) -> None:
    _get(conn, activity_id)
    conn.execute(
        "UPDATE activity SET ended_on = ?, is_ongoing = 0 WHERE id = ?",
        (ended_on, activity_id),
    )


def _get(conn: sqlite3.Connection, activity_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM activity WHERE id = ? AND user_id = ? AND active = 1",
        (activity_id, USER_ID),
    ).fetchone()
    if row is None:
        raise ActivityError(f"no activity {activity_id}")
    return row  # type: ignore[no-any-return]


def list_with_hours(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every active activity with its evidence summary: hours (total-target
    checkpoints only), entry count, and the span of logged dates."""
    rows = conn.execute(
        "SELECT a.*, e.canonical_name AS contact_name, "
        "  COALESCE((SELECT SUM(c.delta) FROM checkpoint c "
        "            JOIN target t ON t.id = c.target_id AND t.kind = 'total' "
        "            WHERE c.activity_id = a.id), 0) AS hours, "
        "  (SELECT COUNT(*) FROM checkpoint c "
        "   JOIN target t ON t.id = c.target_id AND t.kind = 'total' "
        "   WHERE c.activity_id = a.id) AS entry_count, "
        "  (SELECT MIN(c.occurred_at) FROM checkpoint c WHERE c.activity_id = a.id) "
        "   AS first_logged, "
        "  (SELECT MAX(c.occurred_at) FROM checkpoint c WHERE c.activity_id = a.id) "
        "   AS last_logged "
        "FROM activity a LEFT JOIN entity e ON e.id = a.contact_entity_id "
        "WHERE a.user_id = ? AND a.active = 1 "
        "ORDER BY a.most_meaningful DESC, a.category, a.id",
        (USER_ID,),
    ).fetchall()
    return [dict(r) for r in rows]


def entries_for(conn: sqlite3.Connection, activity_id: int) -> list[dict[str, Any]]:
    """The full checkpoint-note stream for one activity — the raw material for its
    AMCAS description, each line traceable to the checkpoint it came from (rule 1)."""
    rows = conn.execute(
        "SELECT c.id, c.occurred_at, c.delta, c.note, t.title AS target_title, t.kind "
        "FROM checkpoint c JOIN target t ON t.id = c.target_id "
        "WHERE c.activity_id = ? ORDER BY c.occurred_at, c.id",
        (activity_id,),
    ).fetchall()
    return [dict(r) for r in rows]
