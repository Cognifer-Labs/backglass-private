"""Rollover and the evening shutdown pass. docs/04 §1.6 and §1.8.

  P10  Rollover items appear at the top of the next day's proposal, above new work.
  P11  An item that rolls over three times is flagged, and the brief asks one question:
       is this actually going to happen, or should it be dropped?
  P12  Rollover count is stored per item and surfaced on the card. "It is the single best
       signal of a commitment that needs renegotiating rather than rescheduling."

On shutdown, docs/04 §1.8 is unusually firm about what *not* to build:

    "Shutdown is optional and skippable. If skipped, the planner infers completion from
    ledger state and marks the rest rollover. **Never nag about a missed shutdown.** A
    productivity system that scolds gets deleted."

So there is no reminder, no streak, no "you missed yesterday". `close_day` runs whether or
not the owner showed up, and the only trace of a skipped shutdown is that `learned` and
`blocked` are null.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID


@dataclass
class CloseReport:
    day: date
    done: int = 0
    rolled: int = 0
    flagged: list[dict[str, Any]] = field(default_factory=list)

    @property
    def anything_happened(self) -> bool:
        return bool(self.done or self.rolled)


def open_blocks(conn: sqlite3.Connection, day: date) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT b.id, b.title, b.commitment_id, b.outcome, b.rollover_count "
        "FROM plan_block b JOIN day_plan p ON p.id = b.day_plan_id "
        "WHERE p.user_id = ? AND p.local_date = ? AND p.status != 'superseded' "
        "  AND b.kind IN ('work', 'protected', 'small')",
        (USER_ID, day.isoformat()),
    ).fetchall()


def close_day(
    conn: sqlite3.Connection,
    settings: Settings,
    day: date,
    *,
    done_block_ids: set[int] | None = None,
) -> CloseReport:
    """End the working day. Anything proposed and not done becomes rollover.

    `done_block_ids` is what the owner confirmed in the shutdown prompt. When it is None
    the shutdown was skipped, and completion is inferred from ledger state — a block whose
    commitment has since been resolved counts as done, everything else rolls.
    """
    report = CloseReport(day=day)

    for block in open_blocks(conn, day):
        block_id = int(block["id"])
        if block["outcome"] != "pending":
            if block["outcome"] == "done":
                report.done += 1
            continue

        if done_block_ids is not None:
            finished = block_id in done_block_ids
        else:
            finished = _commitment_resolved(conn, block["commitment_id"])

        if finished:
            conn.execute("UPDATE plan_block SET outcome = 'done' WHERE id = ?", (block_id,))
            report.done += 1
            continue

        # P12. The count lives on the commitment, and is denormalised onto the block so
        # rendering a card never has to walk history (docs/04 §3).
        count = int(block["rollover_count"] or 0) + 1
        conn.execute(
            "UPDATE plan_block SET outcome = 'rolled', rollover_count = ? WHERE id = ?",
            (count, block_id),
        )
        if block["commitment_id"] is not None:
            conn.execute(
                "UPDATE commitment SET rollover_count = rollover_count + 1 "
                "WHERE id = ? AND status = 'open'",
                (block["commitment_id"],),
            )
        report.rolled += 1

    report.flagged = flagged_for_question(conn, settings)
    return report


def _commitment_resolved(conn: sqlite3.Connection, commitment_id: object) -> bool:
    if commitment_id is None:
        return False
    row = conn.execute(
        "SELECT status FROM commitment WHERE id = ?", (commitment_id,)
    ).fetchone()
    return bool(row and row["status"] in ("done", "dropped", "superseded"))


def flagged_for_question(conn: sqlite3.Connection, settings: Settings) -> list[dict[str, Any]]:
    """P11. Items at the rollover threshold that have not yet been asked about.

    "Triggers the drop-or-do question exactly once" is the acceptance criterion in
    docs/04 §5, so asking is recorded. `resolution_note` carries the marker because it is
    the only free column on `commitment`, and adding a column for a boolean the brief
    reads once a quarter would be the wrong trade.
    """
    # The confidence floor is CLAUDE.md rule 2 and it applies here too: this row is
    # rendered into the brief as a commitment the owner is asked to drop or do. A guess
    # that rolled over three times is still a guess, and asking about it states it as
    # fact — the review queue is where an unconfirmed extraction belongs until the owner
    # accepts it. Every other brief query carries this predicate; this one did not.
    return conn.execute(
        "SELECT c.id, c.what, c.rollover_count, c.due_at, "
        "       s.source, s.external_id, s.occurred_at, s.title "
        "FROM commitment c JOIN source_item s ON s.id = c.source_item_id "
        "WHERE c.user_id = ? AND c.status = 'open' AND c.rollover_count >= ? "
        "  AND c.confidence >= ? "
        "  AND (c.resolution_note IS NULL OR c.resolution_note NOT LIKE 'asked:rollover%')",
        (USER_ID, settings.rollover_question_at, settings.confidence_threshold),
    ).fetchall()


def mark_question_asked(conn: sqlite3.Connection, commitment_ids: list[int]) -> None:
    """Record that the drop-or-do question has been put, so it is asked exactly once."""
    for commitment_id in commitment_ids:
        conn.execute(
            "UPDATE commitment SET resolution_note = ? WHERE id = ?",
            (f"asked:rollover@{now_iso()[:10]}", commitment_id),
        )


def record_shutdown(
    conn: sqlite3.Connection, day: date, *, learned: str | None, blocked: str | None
) -> None:
    """docs/04 §1.8: one line on anything learned or blocked, stored as a note.

    Optional. A day with no note is not a failure and produces no copy anywhere.
    """
    conn.execute(
        "INSERT INTO shutdown_note (user_id, local_date, learned, blocked, created_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (user_id, local_date) DO UPDATE SET "
        "  learned = excluded.learned, blocked = excluded.blocked",
        (USER_ID, day.isoformat(), learned, blocked, now_iso()),
    )
