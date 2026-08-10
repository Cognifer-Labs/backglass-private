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


def _fan_out(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """One message that produced a promise to each of several people.

    Nineteen of the owner's 75 suspect pairs are this, and three of them read
    identically: source item 8763 asked for an intro email to Suriyampola, Hossain and
    Pedram, and extraction wrote all three as "Send instructor intro email from ASU
    address". Presented as two identical sentences and a percentage, that is not a
    question anybody can answer — which is why the queue has 75 open pairs in it.

    Not a reason to hide the pair, and emphatically not a reason to merge it: the same
    shape covers "Complete the Math Placement Test" attributed to two different senders,
    where one task really was read twice. Same source, different person is a fact that
    decides the question, so the owner is told it and still answers.
    """
    return (
        a["source_item_id"] is not None
        and a["source_item_id"] == b["source_item_id"]
        and a["counterparty_entity_id"] != b["counterparty_entity_id"]
    )


def _semantic_pairs(conn: sqlite3.Connection) -> set[tuple[int, int]]:
    """The second opinion, when there is one.

    `entities.similar` compares token sets, which catches restatements that reuse the
    words and misses restatements that do not. Over the owner's 191 open promises it finds
    39 pairs; embeddings find 46, and 13 of those are ones only meaning catches — "Tell
    Mrs. Gathas you are back in Arizona" against "…he's back in Arizona" scores 0.81
    lexically and 0.94 semantically, and is plainly one promise. Six go the other way, so
    neither replaces the other and the union is what gets asked about.

    Silent when nothing is indexed, or when the embedding endpoint is down. Retrieval is
    additive (docs/02) and the board must render without it — an installation that never
    runs `search index` sees exactly the queue it saw before this existed.
    """
    try:
        from backglass import search
        from backglass.config import get_settings

        return search.duplicate_pairs(conn, get_settings())
    except Exception:  # noqa: BLE001 — no index, no endpoint, no vectors: all the same here
        return set()


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
            "SELECT c.id, c.direction, c.what, c.source_item_id,"
            "       c.counterparty_entity_id, e.canonical_name AS who"
            " FROM commitment c"
            " LEFT JOIN entity e ON e.id = c.counterparty_entity_id"
            " WHERE c.user_id = ? AND c.status = 'open' ORDER BY c.id",
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
    semantic = _semantic_pairs(conn)
    out: list[dict[str, Any]] = []
    for i, a in enumerate(rows):
        for b in rows[i + 1 :]:
            if a["direction"] != b["direction"]:
                continue
            pair = (int(a["id"]), int(b["id"]))
            if pair in settled:
                continue
            score = entities.similar(str(a["what"]), str(b["what"]))
            # Agreement is the signal; disagreement is not. Where both call a pair the
            # same thing it is almost always the same thing, and 13 of the owner's pairs
            # are that while scoring under 0.85 lexically — near-certain duplicates
            # sitting anywhere in a queue of 261. The inverse set was measured and
            # discarded: "complete required ASU Ready program" against "…program modules"
            # reads alike and the embedding disagrees, and the embedding is wrong. So this
            # promotes and never demotes.
            by_meaning = pair in semantic
            if score >= SUSPECT_FLOOR or by_meaning:
                out.append(
                    {
                        "a_id": pair[0],
                        "b_id": pair[1],
                        "a_what": str(a["what"]),
                        "b_what": str(b["what"]),
                        "a_who": str(a["who"]) if a["who"] else "",
                        "b_who": str(b["who"]) if b["who"] else "",
                        "score": score,
                        "one_message": _fan_out(a, b),
                        # Which signal found it, because "these read alike" and "these
                        # mean the same" are different claims and the card should not
                        # imply the first when only the second is true.
                        "by_meaning": by_meaning,
                    }
                )
    # Pairs both signals agree on lead, then the rest by wording as before. A queue of 261
    # is one nobody reaches the end of — the same failure the review queue had — so what
    # is nearly certain has to be at the top rather than merely present.
    out.sort(key=lambda p: (not p["by_meaning"], -p["score"]))
    return out
