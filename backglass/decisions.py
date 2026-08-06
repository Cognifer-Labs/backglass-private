"""Major decisions — the choices the owner has settled.

"Sallie Mae application: not doing it" is not an open commitment (nothing left to do),
not a done one (nothing was done), and not quite a fact — it has a lifecycle against
the ledger: recording it should close the open commitment it settles. So it is the
fact pattern (supersession, retraction, provenance in words) plus one optional link
into the commitment ledger.

Rules, inherited from `facts`:

- A decision changes by supersession, never by UPDATE — identity is the normalized
  title, so recording "Sallie Mae application" again replaces the standing decision
  and keeps what it used to say.
- Revisiting is a status, not a DELETE.
- Revisiting does NOT reopen a commitment the decision closed. Reopening by side
  effect is the resurrection bug class; the drop is its own recorded action with its
  own note pointing back here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from backglass.config import Settings
from backglass.ledger import USER_ID
from backglass.plan import timezones


class DecisionError(ValueError):
    pass


@dataclass(frozen=True)
class Decision:
    decision_id: int
    title: str
    choice: str
    reasoning: str | None
    commitment_id: int | None
    commitment_title: str | None
    decided_at: str


def _norm(title: str) -> str:
    """The one normalization for "is this the same decision?".

    Python's, not SQL's: SQLite LOWER() folds ASCII only, and a guard and a lookup
    that answer the same question through different engines is how "Café Latino"
    became permanently unloggable (lessons.md 2026-08-02).
    """
    return " ".join(title.split()).lower()


def record(
    conn: sqlite3.Connection,
    settings: Settings,
    title: str,
    choice: str,
    *,
    reasoning: str | None = None,
    commitment_id: int | None = None,
) -> tuple[int, bool]:
    """Record a decision; the previous active decision with this title is superseded.

    When `commitment_id` names an open commitment, it is dropped in the same
    transaction — the decision settles it. A commitment that is already closed is
    linked but left untouched: the decision is still worth recording, and rewriting
    a closed row's history is not. Returns (decision_id, closed_commitment).
    """
    title, choice = title.strip(), choice.strip()
    if not title or not choice:
        raise DecisionError("a decision needs a title and a choice")

    commitment: dict[str, Any] | None = None
    if commitment_id is not None:
        commitment = conn.execute(
            "SELECT id, status FROM commitment WHERE id = ? AND user_id = ?",
            (commitment_id, USER_ID),
        ).fetchone()
        if commitment is None:
            raise DecisionError(f"no commitment {commitment_id}")

    now = timezones.local_now_iso(settings)
    cur = conn.execute(
        "INSERT INTO decision (user_id, title, choice, reasoning, commitment_id,"
        " status, decided_at, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
        (
            USER_ID,
            title,
            choice,
            (reasoning or "").strip() or None,
            commitment_id,
            now,
            now,
        ),
    )
    new_id = int(cur.lastrowid or 0)

    # Supersession by normalized title, compared in Python — the active set is small.
    previous = conn.execute(
        "SELECT id, title FROM decision WHERE user_id = ? AND status = 'active' AND id != ?",
        (USER_ID, new_id),
    ).fetchall()
    stale = [int(r["id"]) for r in previous if _norm(str(r["title"])) == _norm(title)]
    if stale:
        conn.executemany(
            "UPDATE decision SET status = 'superseded', superseded_by = ? WHERE id = ?",
            [(new_id, sid) for sid in stale],
        )

    closed = False
    if commitment is not None and str(commitment["status"]) == "open":
        conn.execute(
            "UPDATE commitment SET status = 'dropped', resolved_at = ?,"
            " resolution_note = ? WHERE id = ? AND status = 'open'",
            (now, f"decision:{new_id} — {choice}", commitment_id),
        )
        closed = True
    return new_id, closed


def active(conn: sqlite3.Connection) -> list[Decision]:
    """Standing decisions, newest first — the list the page and the CLI both print."""
    rows = conn.execute(
        "SELECT d.id, d.title, d.choice, d.reasoning, d.commitment_id, d.decided_at,"
        " c.what AS commitment_title"
        " FROM decision d LEFT JOIN commitment c ON c.id = d.commitment_id"
        " WHERE d.user_id = ? AND d.status = 'active'"
        " ORDER BY d.decided_at DESC, d.id DESC",
        (USER_ID,),
    ).fetchall()
    return [
        Decision(
            decision_id=int(r["id"]),
            title=str(r["title"]),
            choice=str(r["choice"]),
            reasoning=r["reasoning"],
            commitment_id=r["commitment_id"],
            commitment_title=r["commitment_title"],
            decided_at=str(r["decided_at"]),
        )
        for r in rows
    ]


def revisit(conn: sqlite3.Connection, decision_id: int) -> None:
    """Withdraw a decision — the row survives; only its claim is retracted.

    The commitment it closed stays closed (see module docstring for why).
    """
    cur = conn.execute(
        "UPDATE decision SET status = 'retracted'"
        " WHERE id = ? AND user_id = ? AND status = 'active'",
        (decision_id, USER_ID),
    )
    if cur.rowcount == 0:
        raise DecisionError(f"no active decision {decision_id}")
