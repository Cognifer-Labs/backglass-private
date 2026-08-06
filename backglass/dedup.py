"""Likely-duplicate open commitments, offered as a question rather than merged.

The extraction dedup joins restatements scoring at or above `dedup_threshold`
automatically. Below it sits a band the similarity function cannot settle — the same
plan described in mail, in a group chat and in a quick-add, phrased differently
enough that 0.85 never fires. Live ledger evidence: six clusters, ~13 rows, surviving
every sync. Those are a question for the owner: same thing, or different? The answer
is remembered either way — a merge on the loser row itself, a "different" in
`commitment_distinct` — so no pair is ever asked about twice.

Same-direction pairs only, but ANY counterparty: the whole reason two rows of one
promise coexist is that two sources resolved the person differently (or not at all),
so requiring an entity match would hide exactly the pairs this exists to find.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from backglass.extract import entities
from backglass.ledger import USER_ID

#: Below this the pairs are noise — nearly everything scores a little alike.
SUSPECT_FLOOR = 0.6


def suspects(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Open same-direction pairs scoring in [SUSPECT_FLOOR, 1.0], strongest first.

    Pairs at or above `dedup_threshold` are included too: rows inserted before the
    dedup matured (or through quick-add, which never dedups) persist above the
    threshold, and "the auto-joiner would have caught this today" is exactly the
    strongest kind of suspect. O(n²) over open commitments — n is a personal ledger's
    open set, double digits, and `similar` is stdlib difflib; measured in
    milliseconds, computed at read time so there is nothing to fall out of date.
    """
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT id, direction, what FROM commitment"
            " WHERE user_id = ? AND status = 'open' ORDER BY id",
            (USER_ID,),
        )
    ]
    settled = {
        (int(r["low_id"]), int(r["high_id"]))
        for r in conn.execute(
            "SELECT low_id, high_id FROM commitment_distinct WHERE user_id = ?",
            (USER_ID,),
        )
    }
    out: list[dict[str, Any]] = []
    for i, a in enumerate(rows):
        for b in rows[i + 1 :]:
            if a["direction"] != b["direction"]:
                continue
            pair = (int(a["id"]), int(b["id"]))
            if pair in settled:
                continue
            score = entities.similar(str(a["what"]), str(b["what"]))
            if score >= SUSPECT_FLOOR:
                out.append(
                    {
                        "a_id": pair[0],
                        "b_id": pair[1],
                        "a_what": str(a["what"]),
                        "b_what": str(b["what"]),
                        "score": score,
                    }
                )
    out.sort(key=lambda p: -p["score"])
    return out
