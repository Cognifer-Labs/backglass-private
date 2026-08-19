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
    #: True only when THIS decision dropped the commitment. A decision may link a
    #: commitment that was already closed; rendering that as "closed:" would claim
    #: an act that never happened.
    closed_commitment: bool
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

    now = timezones.local_now_iso(settings)
    # One transaction for the whole act. Connections are autocommit
    # (db/__init__.py: isolation_level=None), so without this the INSERT, the
    # supersession UPDATE and the commitment drop each commit alone — an interrupt
    # between them leaves a standing decision whose commitment is still open, or two
    # active decisions sharing one title. Same pattern as people/merge.py.
    conn.execute("BEGIN IMMEDIATE")
    try:
        commitment: dict[str, Any] | None = None
        if commitment_id is not None:
            commitment = conn.execute(
                "SELECT id, status FROM commitment WHERE id = ? AND user_id = ?",
                (commitment_id, USER_ID),
            ).fetchone()
            if commitment is None:
                raise DecisionError(f"no commitment {commitment_id}")

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
            "SELECT id, title FROM decision"
            " WHERE user_id = ? AND status = 'active' AND id != ?",
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
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return new_id, closed


#: What the logic checker writes its `reasoning` behind. Machine disposals are recorded
#: here because this table is where provenance and supersession already live — but they
#: are bookkeeping, not deliberation, and the first live pass wrote fourteen of them
#: against the owner's four standing decisions. A page that files "Disposed of commitment
#: 71" next to "DECLINED the Aug 5 early move-in" stops being the record of what the
#: owner decided, so the two are separated on read rather than at write.
MACHINE_PREFIX = "logic: "


def active(conn: sqlite3.Connection) -> list[Decision]:
    """Standing decisions the OWNER made, newest first — the page and the CLI's list.

    Machine disposals are excluded and read back through `disposals()`. Same table, same
    provenance; different question. This one answers "what have I decided", and an
    automatic tidy-up is not an answer to it.
    """
    rows = conn.execute(
        "SELECT d.id, d.title, d.choice, d.reasoning, d.commitment_id, d.decided_at,"
        " c.what AS commitment_title, c.resolution_note AS commitment_note"
        " FROM decision d LEFT JOIN commitment c ON c.id = d.commitment_id"
        " WHERE d.user_id = ? AND d.status = 'active'"
        "   AND (d.reasoning IS NULL OR d.reasoning NOT LIKE ?)"
        " ORDER BY d.decided_at DESC, d.id DESC",
        (USER_ID, MACHINE_PREFIX + "%"),
    ).fetchall()
    return [
        Decision(
            decision_id=int(r["id"]),
            title=str(r["title"]),
            choice=str(r["choice"]),
            reasoning=r["reasoning"],
            commitment_id=r["commitment_id"],
            commitment_title=r["commitment_title"],
            closed_commitment=str(r["commitment_note"] or "").startswith(
                f"decision:{r['id']} "
            ),
            decided_at=str(r["decided_at"]),
        )
        for r in rows
    ]


def disposals(conn: sqlite3.Connection, limit: int = 20) -> list[Decision]:
    """What the logic checker threw out, newest first.

    The other half of `active()`, and the reason automatic disposal is survivable at all:
    a row that disappears with nothing anywhere saying why is indistinguishable from a
    bug, and the owner would be right to stop trusting the board. Each of these names the
    rule and the contradiction, and the commitment behind it is tombstoned, not deleted.
    """
    rows = conn.execute(
        "SELECT d.id, d.title, d.choice, d.reasoning, d.commitment_id, d.decided_at,"
        " c.what AS commitment_title, c.resolution_note AS commitment_note"
        " FROM decision d LEFT JOIN commitment c ON c.id = d.commitment_id"
        " WHERE d.user_id = ? AND d.status = 'active' AND d.reasoning LIKE ?"
        " ORDER BY d.decided_at DESC, d.id DESC LIMIT ?",
        (USER_ID, MACHINE_PREFIX + "%", limit),
    ).fetchall()
    return [
        Decision(
            decision_id=int(r["id"]),
            title=str(r["title"]),
            choice=str(r["choice"]),
            reasoning=r["reasoning"],
            commitment_id=r["commitment_id"],
            commitment_title=r["commitment_title"],
            closed_commitment=False,
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
