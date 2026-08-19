"""When an obligation has quietly stopped being true, and what may still act on it.

One predicate, two readers. `questions._stale_commitments` turns it into "still real?";
`plan.planner.candidates` uses it to leave those rows out of the day. They must be the
same rule: asking whether something is still real while scheduling half an hour for it is
the state the owner saw on 2026-08-18 — four of twelve blocks given to a UT Dallas
scholarship deadline that lapsed on 2026-05-01, for a school he does not attend.

Staleness here is deliberately weak evidence: past due, and nothing in the ledger has
mentioned it since. That is enough to *stop asserting* a commitment, which is what
scheduling it does, and nowhere near enough to close it — mail has no thread to re-read,
so silence is even weaker evidence than in chat (`extract/recheck.py`). Nothing in this
module writes.

The owner's answer outranks the rule. A `stale` question answered `STALE_KEEP` — "still
on my plate" — takes its commitment out of the stale set permanently, so the next plan
schedules it again. Without that, "keep it open" would keep it open and invisible, which
is the worst of the three answers to honour badly.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from backglass.ledger import USER_ID

#: A commitment this far past due, with nothing in the ledger mentioning it since, is a
#: candidate for "quietly no longer true". Two weeks, because the board already nags
#: inside that window and the recheck pass owns conversations; this is for the
#: mail-shaped obligation that decays with no thread to re-read.
STALE_OVERDUE_DAYS = 14
STALE_SILENCE_DAYS = 14

#: New stale questions per refresh. The first run against a neglected board would
#: otherwise raise a hundred at once, and a wall of questions is how an owner stops
#: answering any (the overflow-list lesson, 2026-08-09). Oldest first; the rest queue
#: behind answers. The planner gate has no such limit on purpose — the queue is paced by
#: how many questions a person will answer, and the plan must be right today.
STALE_BATCH_LIMIT = 5

#: The stale question's options, matched EXACTLY by the answer hook — a reworded option
#: is an answer the hook cannot act on, so these are constants, not prose.
STALE_DONE = "Done — mark it resolved"
STALE_DROP = "No longer relevant — drop it"
STALE_KEEP = "Still on my plate — keep it open"

#: Newest evidence per commitment: MAX over datetime() of its own source item and every
#: `commitment_evidence` sighting — datetime(), not the raw column, because occurred_at
#: keeps each sender's own offset and MAX over text picks the wrong message across the
#: owner's two zones (lessons, 2026-08-02).
_STALE_SQL = """
SELECT c.id, c.what, substr(c.due_at, 1, 10) AS due_day, c.direction,
       last.seen_day, last.source, last.title
FROM commitment c
JOIN (
  SELECT ranked.commitment_id,
         substr(ranked.occurred_at, 1, 10) AS seen_day,
         ranked.source AS source, ranked.title AS title
  FROM (
    SELECT c2.id AS commitment_id, si.occurred_at, si.source, si.title,
           ROW_NUMBER() OVER (
             PARTITION BY c2.id ORDER BY datetime(si.occurred_at) DESC
           ) AS rn
    FROM commitment c2
    JOIN source_item si
      ON si.id = c2.source_item_id
      OR si.id IN (
           SELECT ce.source_item_id FROM commitment_evidence ce
           WHERE ce.commitment_id = c2.id AND ce.user_id = c2.user_id
         )
    WHERE c2.user_id = :user_id AND c2.status = 'open'
  ) ranked
  WHERE ranked.rn = 1
) last ON last.commitment_id = c.id
WHERE c.user_id = :user_id
  AND c.status = 'open'
  AND c.due_at IS NOT NULL
  AND substr(c.due_at, 1, 10) <= :overdue_floor
  AND last.seen_day <= :silence_floor
  AND c.id NOT IN (
        SELECT CAST(q.subject_key AS INTEGER) FROM open_question q
        WHERE q.user_id = :user_id AND q.kind = 'stale'
          AND q.status = 'answered' AND q.answer_option = :keep
      )
ORDER BY substr(c.due_at, 1, 10) ASC, c.id ASC
"""


def stale_rows(
    conn: sqlite3.Connection, today: date, *, limit: int | None = None
) -> list[Any]:
    """Open commitments long past due that nothing has mentioned since, oldest first.

    `limit` is the question surface's batch cap. The planner passes none: every stale row
    leaves the day, whether or not it has been asked about yet.
    """
    sql = _STALE_SQL + ("LIMIT :limit" if limit is not None else "")
    return list(
        conn.execute(
            sql,
            {
                "user_id": USER_ID,
                "overdue_floor": (today - timedelta(days=STALE_OVERDUE_DAYS)).isoformat(),
                "silence_floor": (today - timedelta(days=STALE_SILENCE_DAYS)).isoformat(),
                "keep": STALE_KEEP,
                **({"limit": limit} if limit is not None else {}),
            },
        )
    )


def stale_ids(conn: sqlite3.Connection, today: date) -> set[int]:
    """The commitment ids the planner may not schedule. Read-only, never raises on empty."""
    return {int(row["id"]) for row in stale_rows(conn, today)}
