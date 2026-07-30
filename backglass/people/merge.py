"""Entity merge: one person, two rows, made one. Phase 6.

The loser's aliases — plus its canonical name — fold into the winner, so
`Ledger.resolve_entity` finds the winner by every address and name that ever
reached either row. Commitments repoint; checkpoints have no entity reference
(ruling in tasks/todo.md §Phase 6). The loser row is deleted, but its full
snapshot lands in `entity_merge` first: undo is manual, but never impossible.

Connections are autocommit (db.connect sets isolation_level=None), so the merge
takes an explicit transaction — a half-merged pair of rows is worse than either
input.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from backglass.db import now_iso
from backglass.ledger import USER_ID


class MergeError(RuntimeError):
    pass


def merge(conn: sqlite3.Connection, winner_id: int, loser_id: int) -> dict[str, Any]:
    if winner_id == loser_id:
        raise MergeError("cannot merge an entity into itself")
    winner = conn.execute(
        "SELECT * FROM entity WHERE id = ? AND user_id = ?", (winner_id, USER_ID)
    ).fetchone()
    loser = conn.execute(
        "SELECT * FROM entity WHERE id = ? AND user_id = ?", (loser_id, USER_ID)
    ).fetchone()
    if winner is None:
        raise MergeError(f"no entity {winner_id}")
    if loser is None:
        raise MergeError(f"no entity {loser_id}")

    aliases = list(dict.fromkeys(  # order-preserving union
        json.loads(winner["aliases_json"] or "[]")
        + json.loads(loser["aliases_json"] or "[]")
        + [loser["canonical_name"]]
    ))
    tags = sorted(
        set(json.loads(winner["tags_json"] or "[]"))
        | set(json.loads(loser["tags_json"] or "[]"))
    )
    notes = "\n\n".join(n for n in (winner["notes"], loser["notes"]) if n) or None

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE entity SET aliases_json = ?, tags_json = ?, notes = ?, "
            "role = COALESCE(role, ?), org = COALESCE(org, ?), updated_at = ? WHERE id = ?",
            (json.dumps(aliases), json.dumps(tags), notes,
             loser["role"], loser["org"], now_iso(), winner_id),
        )
        repointed = conn.execute(
            "UPDATE commitment SET counterparty_entity_id = ? WHERE counterparty_entity_id = ?",
            (winner_id, loser_id),
        ).rowcount
        conn.execute(
            "INSERT INTO entity_merge (user_id, winner_id, loser_snapshot_json, merged_at)"
            " VALUES (?, ?, ?, ?)",
            (USER_ID, winner_id, json.dumps(dict(loser)), now_iso()),
        )
        conn.execute("DELETE FROM entity WHERE id = ?", (loser_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"winner_id": winner_id, "commitments_repointed": repointed, "aliases": aliases}
