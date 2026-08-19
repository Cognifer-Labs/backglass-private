"""Things the record already contradicts, disposed of instead of asked about.

The owner, 2026-08-18, after a plan that gave four blocks to a scholarship deadline at a
school he does not attend: *"we also need a logic checker for commitments and questions,
if something obviously doesnt make sense then dispose of it yourself."*

That is a ruling, and it needs a boundary or it becomes the silent-data-loss failure four
lessons were written about. The boundary is **positive contradiction**, never silence:

- `extract/recheck.py` closes a chat promise only on a quoted later message.
- `staleness.py` never closes at all — it stops *scheduling* and asks.
- This module closes only where the ledger disagrees with itself: an obligation whose own
  text reports it already happened, a question about a day that has ended, a question
  about a commitment that is no longer open.

Nothing here reasons from "nobody mentioned it since". A rule that cannot point at the
row that contradicts the row it is closing does not belong in this file.

Two properties keep it reversible, because an automatic disposal nobody can find is worse
than a wrong one they can:

**Every disposal is provenanced.** A dropped or resolved commitment carries a
`resolution_note` starting `logic:` naming the rule, and a `decision` row records it in
the owner's own audit trail. Commitments are tombstoned, never deleted (docs/03).

**A mooted question is revivable.** The owner's `dismiss` is permanent by design — "a
question the owner has waved away twice is noise" — but a question this module retires
because the world moved on must come back if the world moves again: two classes that stop
colliding in August and collide again in January are a live question the second time.
`moot` is therefore its own status, and `questions.refresh` re-raises through it.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID

#: An obligation whose text is a report of it having happened. The extractor writes these
#: when a mail says "sent the updated resume" and the reader takes the sentence as a
#: promise: the board then owes the owner something they already did.
#:
#: Anchored at the start, and to verbs that only report. "zip it up once done" (commitment
#: 48 on the live ledger) is a real future obligation that ends in "done" and must not
#: match — which is why there is no trailing-"done" rule here, only trailing "completed"
#: after a subject.
_REPORTED_DONE = re.compile(
    r"^\s*(sent|replied|responded|submitted|emailed|mailed|uploaded|delivered|shared)\b"
    r"|\bcompleted\s*$",
    re.I,
)


@dataclass
class Disposal:
    """One thing disposed of, and the row that contradicts it."""

    kind: str          # commitment|question
    subject_id: int
    rule: str
    action: str        # resolved|dropped|mooted
    reason: str

    def line(self) -> str:
        return f"{self.kind} {self.subject_id}: {self.action} — {self.reason}"


@dataclass
class Report:
    disposals: list[Disposal] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def applied(self) -> int:
        return len(self.disposals)

    def by_rule(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.disposals:
            out[d.rule] = out.get(d.rule, 0) + 1
        return out


# ── rules ──────────────────────────────────────────────────────────────────
#
# Each returns the disposals it would make and writes nothing. They are separate
# functions for rule 5's reason: one rule that raises costs one rule, not the pass.


def _reported_done(conn: sqlite3.Connection) -> list[Disposal]:
    """"sent updated resume" is not something you owe. It is something you did.

    Resolved rather than dropped: the work happened, and a dropped row would tell next
    year's reader the owner walked away from it.
    """
    out: list[Disposal] = []
    for row in conn.execute(
        "SELECT id, what FROM commitment WHERE user_id = ? AND status = 'open'"
        " AND direction = 'i_owe' ORDER BY id",
        (USER_ID,),
    ):
        what = str(row["what"])
        if _REPORTED_DONE.search(what):
            out.append(
                Disposal(
                    kind="commitment",
                    subject_id=int(row["id"]),
                    rule="reported-done",
                    action="resolved",
                    reason=f'"{what}" reports the thing as already done, not as owed',
                )
            )
    return out


def _questions_about_closed_commitments(conn: sqlite3.Connection) -> list[Disposal]:
    """A "still real?" about a commitment that is no longer open answers itself.

    Covers the `stale` kind and the protected-time half of `priority`, both of which key
    on a commitment id. A question the owner can no longer act on is not a question.
    """
    out: list[Disposal] = []
    for row in conn.execute(
        "SELECT q.id, q.kind, q.subject_key FROM open_question q"
        " WHERE q.user_id = ? AND q.status = 'open'"
        "   AND (q.kind = 'stale' OR q.subject_key LIKE 'protected|%')"
        " ORDER BY q.id",
        (USER_ID,),
    ):
        key = str(row["subject_key"])
        raw = key.split("|")[1] if key.startswith("protected|") else key
        try:
            commitment_id = int(raw)
        except ValueError:
            continue
        status = conn.execute(
            "SELECT status FROM commitment WHERE id = ? AND user_id = ?",
            (commitment_id, USER_ID),
        ).fetchone()
        if status is None or str(status["status"]) == "open":
            continue
        out.append(
            Disposal(
                kind="question",
                subject_id=int(row["id"]),
                rule="question-about-a-closed-commitment",
                action="mooted",
                reason=(
                    f"commitment {commitment_id} is {status['status']}; "
                    "the question cannot be acted on"
                ),
            )
        )
    return out


def _questions_about_days_that_ended(
    conn: sqlite3.Connection, today: date
) -> list[Disposal]:
    """"This did not fit today. Should it have come first?" about a day that has ended.

    `_priority` keys on `{day}|{commitment_id}`, so the day is in the identity and the
    check is a date comparison — no re-detection, no guessing. Asked about Tuesday on
    Tuesday it is a real question; asked on Friday it is archaeology.
    """
    out: list[Disposal] = []
    for row in conn.execute(
        "SELECT id, subject_key FROM open_question WHERE user_id = ? AND status = 'open'"
        "  AND kind = 'priority' ORDER BY id",
        (USER_ID,),
    ):
        head = str(row["subject_key"]).split("|")[0]
        try:
            asked_about = date.fromisoformat(head)
        except ValueError:
            continue  # the protected-time form; `_questions_about_closed_commitments` owns it
        if asked_about >= today:
            continue
        out.append(
            Disposal(
                kind="question",
                subject_id=int(row["id"]),
                rule="question-about-a-day-that-ended",
                action="mooted",
                reason=(
                    f"{asked_about.isoformat()} is over; "
                    "the plan it asks about cannot change"
                ),
            )
        )
    return out


def _questions_the_calendar_no_longer_supports(
    conn: sqlite3.Connection, settings: Settings, today: date
) -> list[Disposal]:
    """A collision that is no longer on the calendar, or a placeholder hour that is gone.

    Only `conflict` and `untitled`, and only those two detectors are re-run. Their output
    is a pure function of the next `HORIZON_DAYS` of events, so absence really is proof:
    the class was dropped, the event was named, or the day passed out of the window.

    Deliberately not applied to `priority` or `protected` questions — those are functions
    of one day's transient overflow, so today's silence proves nothing about them, and
    mooting on it would retire live questions every time capacity moved.
    """
    from backglass import questions as questions_mod

    live: set[tuple[str, str]] = set()
    for detector in (questions_mod._conflicts, questions_mod._untitled):
        for question in detector(conn, settings, today):
            live.add((question.kind, question.subject_key))

    out: list[Disposal] = []
    for row in conn.execute(
        "SELECT id, kind, subject_key FROM open_question WHERE user_id = ? AND"
        "  status = 'open' AND kind IN ('conflict', 'untitled') ORDER BY id",
        (USER_ID,),
    ):
        if (str(row["kind"]), str(row["subject_key"])) in live:
            continue
        out.append(
            Disposal(
                kind="question",
                subject_id=int(row["id"]),
                rule="question-the-calendar-dropped",
                action="mooted",
                reason="the events behind it are no longer in the next three weeks",
            )
        )
    return out


def check(conn: sqlite3.Connection, settings: Settings, today: date) -> Report:
    """Every rule, each failing on its own. Read-only: `check` decides, `apply` writes."""
    report = Report()
    for rule in (
        lambda: _reported_done(conn),
        lambda: _questions_about_closed_commitments(conn),
        lambda: _questions_about_days_that_ended(conn, today),
        lambda: _questions_the_calendar_no_longer_supports(conn, settings, today),
    ):
        try:
            report.disposals.extend(rule())
        except Exception as exc:  # noqa: BLE001 — one rule down is not the pass down
            report.errors.append(str(exc))
    return report


# ── writing ────────────────────────────────────────────────────────────────


def apply(
    conn: sqlite3.Connection,
    settings: Settings,
    report: Report,
    *,
    dry_run: bool = False,
) -> Report:
    """Dispose of what `check` found, through the same actions the board uses.

    Idempotent by construction (rule 3): every rule reads a state its own disposal ends,
    so a second pass over an unchanged ledger finds nothing and writes nothing.
    """
    if dry_run:
        return report

    from backglass import decisions
    from backglass.web import actions

    for disposal in list(report.disposals):
        note = f"logic: {disposal.rule} — {disposal.reason}"
        try:
            if disposal.kind == "commitment":
                if disposal.action == "resolved":
                    actions.resolve(conn, disposal.subject_id, note=note)
                else:
                    actions.drop(conn, disposal.subject_id, note=note)
            else:
                moot(conn, disposal.subject_id, note)
        except actions.ActionError as exc:
            # Closed by another surface between check and apply. The disposal simply did
            # not happen; it is not an error worth failing a sync over.
            report.disposals.remove(disposal)
            report.errors.append(f"{disposal.line()}: {exc}")
            continue
        decisions.record(
            conn,
            settings,
            title=f"Disposed of {disposal.kind} {disposal.subject_id}",
            choice=disposal.action,
            reasoning=note,
        )
    return report


def moot(conn: sqlite3.Connection, question_id: int, note: str) -> None:
    """Retire a question the world has answered by moving on.

    `moot`, not `dismissed`. The owner's dismissal is permanent on purpose; this one must
    come back if the same collision reappears, and `questions.refresh` re-raises through
    this status for exactly that case.
    """
    conn.execute(
        "UPDATE open_question SET status = 'moot', answer_text = ?, answered_at = ?"
        " WHERE id = ? AND user_id = ? AND status = 'open'",
        (note, now_iso(), question_id, USER_ID),
    )


def run(
    conn: sqlite3.Connection,
    settings: Settings,
    today: date,
    *,
    dry_run: bool = False,
) -> Report:
    return apply(conn, settings, check(conn, settings, today), dry_run=dry_run)


def recent(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """What the checker disposed of lately, for the surface that has to show its work."""
    return [
        dict(row)
        for row in conn.execute(
            "SELECT title, choice, reasoning, decided_at FROM decision"
            " WHERE user_id = ? AND reasoning LIKE 'logic: %' AND superseded_by IS NULL"
            " ORDER BY id DESC LIMIT ?",
            (USER_ID, limit),
        )
    ]
