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
        # Every table that names an entity has to be repointed before the DELETE below,
        # or the foreign key refuses and the whole merge rolls back. engagement_person
        # is UNIQUE on (user_id, engagement_id, entity_id), so a plan both halves of the
        # duplicate already attend cannot simply be updated onto the winner — that row
        # already exists. Drop the loser's link in that case and repoint the rest; the
        # guest list is a set, and the winner is already in it.
        conn.execute(
            "DELETE FROM engagement_person WHERE user_id = ? AND entity_id = ? "
            "  AND engagement_id IN ("
            "    SELECT engagement_id FROM engagement_person"
            "     WHERE user_id = ? AND entity_id = ?)",
            (USER_ID, loser_id, USER_ID, winner_id),
        )
        plans_repointed = conn.execute(
            "UPDATE engagement_person SET entity_id = ? WHERE user_id = ? AND entity_id = ?",
            (winner_id, USER_ID, loser_id),
        ).rowcount
        # The remaining two references to entity, neither of which was ever repointed —
        # both predate engagements and both were live 500s on the People page.
        #
        # An activity's contact is a person; when that person turns out to be a duplicate,
        # the activity's contact is the survivor.
        conn.execute(
            "UPDATE activity SET contact_entity_id = ? "
            "WHERE user_id = ? AND contact_entity_id = ?",
            (winner_id, USER_ID, loser_id),
        )
        # And a merge audit row names the entity that won. Merging a past winner into
        # someone else — the second merge in a cleanup pass, which is exactly when it
        # happens — left that row pointing at a row about to be deleted. The history now
        # belongs to whoever survives, which is also the truthful reading: this is where
        # those aliases ended up.
        conn.execute(
            "UPDATE entity_merge SET winner_id = ? WHERE user_id = ? AND winner_id = ?",
            (winner_id, USER_ID, loser_id),
        )
        # Recorded touches (migration 0023) follow the person, not the row. The unique
        # index is (user_id, entity_id, kind, day), so two halves of a duplicate that
        # were both logged as met on the same day collide — the loser's copy is dropped
        # rather than repointed, which is right on the merits too: it was one meeting,
        # recorded twice because the person was two rows.
        conn.execute(
            "DELETE FROM touchpoint WHERE user_id = ? AND entity_id = ?"
            "  AND EXISTS (SELECT 1 FROM touchpoint w WHERE w.user_id = touchpoint.user_id"
            "    AND w.entity_id = ? AND w.kind = touchpoint.kind"
            "    AND substr(w.occurred_at, 1, 10) = substr(touchpoint.occurred_at, 1, 10))",
            (USER_ID, loser_id, winner_id),
        )
        conn.execute(
            "UPDATE touchpoint SET entity_id = ? WHERE user_id = ? AND entity_id = ?",
            (winner_id, USER_ID, loser_id),
        )
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
    return {
        "winner_id": winner_id,
        "commitments_repointed": repointed,
        "plans_repointed": plans_repointed,
        "aliases": aliases,
    }
