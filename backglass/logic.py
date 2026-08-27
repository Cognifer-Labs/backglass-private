"""Things the record already contradicts, disposed of instead of asked about.

The owner, 2026-08-18, after a plan that gave four blocks to a scholarship deadline at a
school he does not attend: *"we also need a logic checker for commitments and questions,
if something obviously doesnt make sense then dispose of it yourself."*

That is a ruling, and it needs a boundary or it becomes the silent-data-loss failure four
lessons were written about. The boundary is **positive contradiction**, never silence:

- `extract/recheck.py` closes a chat promise only on a quoted later message.
- `staleness.py` never closes at all — it stops *scheduling* and asks.
- This module closes only where the ledger disagrees with itself: an obligation whose own
  text reports it already happened, an obligation to attend something on a day that has
  ended, a question about a day that has ended, a question about a commitment that is no
  longer open.

Nothing here reasons from "nobody mentioned it since". A rule that cannot point at the
row that contradicts the row it is closing does not belong in this file.

**The one exception, and it is an owner ruling rather than a contradiction.**
`_canvas_assignments_past_grace` closes a *deliverable* on elapsed time alone. It is here
because the Canvas ICS feed structurally cannot report submission (docs/07 §Canvas), so
those rows can never close themselves and 141 of them landed in a single afternoon; the
owner ruled on 2026-08-20 that time should close them. It is bounded by source, not by the
shape of the sentence, and it is the only place in this file where absence of evidence is
allowed to act. Do not read it as a precedent for the rest.

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
from datetime import date, timedelta
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

#: An obligation to *be somewhere at a time*, which the time passing answers by itself.
#: The owner, 2026-08-20: *"it is planning for things that are obviously done, for example
#: i already moved in on the 9th"* — commitment 8, `Move-in: Willow Hall 502, 8:00am`, due
#: 2026-08-09 and still open eleven days later because `staleness` waits fourteen and then
#: only asks.
#:
#: Attendance, and nothing else. "Submit the housing contract" is past due and still owed;
#: closing it would be the silent data loss this module's docstring exists to forbid. The
#: separation is the whole safety of the rule, so the verbs are the narrow, physical ones:
#: you cannot still owe your presence at something that already happened.
#:
#: `move-in` is matched anywhere because the owner's own rows write it as a label
#: ("Move-in: Willow Hall 502"), not as a verb in a sentence.
_ATTENDANCE = re.compile(
    r"^\s*(attend|arrive|check\s*in|go\s+to|show\s+up|be\s+at|drop\s+in)\b"
    r"|\bmove[\s-]?in\b",
    re.I,
)

#: How long a Canvas assignment stays owed after its due date. The owner ruled on
#: 2026-08-20 that these expire: the ICS feed carries no submission state (docs/07
#: §Canvas), so an assignment already handed in reads `open` forever, and 141 of them
#: landed in one afternoon. Seven days clears the late-submission window most courses
#: allow while still closing the row long before `staleness` would think to ask.
#:
#: Scoped to `canvas:ics` by source, never by shape. This is the one place the module
#: closes a *deliverable*, it does so only under an explicit ruling, and it must not
#: generalise to anything the owner did not rule on.
CANVAS_GRACE_DAYS = 7


@dataclass
class Disposal:
    """One thing disposed of, and the row that contradicts it."""

    kind: str          # commitment|question
    subject_id: int
    rule: str
    action: str        # resolved|dropped|mooted|moved
    reason: str
    #: Only `moved` carries one: the new due date. A move is the one verb here that
    #: changes a row instead of closing it, and it needs somewhere to put the value it
    #: is changing to. Everything else leaves this None.
    new_value: str | None = None

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

    Covers the `stale` kind, the protected-time half of `priority`, and
    `duplicate_commitment`. A question the owner can no longer act on is not a question.

    A duplicate card is mooted when **either** side has closed, not both, and that is the
    difference that makes it correct: the card asks "are these two the same promise?" and
    answering it runs `actions.same_thing`, which needs two open rows to merge. With one
    of them dropped the answer cannot do anything — found the same day the kind shipped,
    when the relevance judge dropped two rows that duplicate cards were already asking
    about, and the cards stayed on the board as dead weight.
    """
    out: list[Disposal] = []
    for row in conn.execute(
        "SELECT q.id, q.kind, q.subject_key FROM open_question q"
        " WHERE q.user_id = ? AND q.status = 'open'"
        "   AND (q.kind IN ('stale', 'duplicate_commitment')"
        "        OR q.subject_key LIKE 'protected|%')"
        " ORDER BY q.id",
        (USER_ID,),
    ):
        key = str(row["subject_key"])
        if str(row["kind"]) == "duplicate_commitment":
            disposal = _dead_duplicate(conn, int(row["id"]), key)
            if disposal is not None:
                out.append(disposal)
            continue
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


def _dead_duplicate(
    conn: sqlite3.Connection, question_id: int, key: str
) -> Disposal | None:
    """One duplicate card, if the pair it names can no longer be merged."""
    try:
        a_id, b_id = (int(part) for part in key.split("|"))
    except ValueError:
        return None
    rows = {
        int(r["id"]): str(r["status"])
        for r in conn.execute(
            "SELECT id, status FROM commitment WHERE user_id = ? AND id IN (?, ?)",
            (USER_ID, a_id, b_id),
        )
    }
    closed = [
        f"{cid} is {rows.get(cid, 'gone')}"
        for cid in (a_id, b_id)
        if rows.get(cid, "gone") != "open"
    ]
    if not closed:
        return None
    return Disposal(
        kind="question",
        subject_id=question_id,
        rule="duplicate-pair-no-longer-mergeable",
        action="mooted",
        reason=f"{' and '.join(closed)}; a merge needs two open rows",
    )


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


def _events_whose_day_has_passed(
    conn: sqlite3.Connection, today: date
) -> list[Disposal]:
    """Being somewhere on a day that is over is not an open obligation.

    The same rule shape as `_questions_about_days_that_ended`, one table across: the day
    is in the row, the check is a date comparison, and there is no re-detection and no
    guessing. Positive contradiction, because a date that has passed is a fact the ledger
    holds about the row it closes — not "nobody has mentioned it since".

    Strictly `<` today, so an event this morning survives until tomorrow. The owner moves
    between UTC-7 and UTC+5:30 and `today` is resolved in their local zone by the caller;
    a same-day comparison would retire tonight's obligations from the other side of the
    world.

    Dropped rather than resolved, and the wording of the note is the reason. Backglass does
    not know whether the owner attended — only that the hour is gone — and `_reported_done`
    resolves precisely because there the ledger *says* the work happened. Claiming `done`
    here would put a fact in the record that nothing supports.
    """
    out: list[Disposal] = []
    for row in conn.execute(
        "SELECT id, what, due_at FROM commitment"
        " WHERE user_id = ? AND status = 'open' AND due_at IS NOT NULL ORDER BY id",
        (USER_ID,),
    ):
        what = str(row["what"])
        if not _ATTENDANCE.search(what):
            continue
        try:
            due = date.fromisoformat(str(row["due_at"])[:10])
        except ValueError:
            continue
        if due >= today:
            continue
        out.append(
            Disposal(
                kind="commitment",
                subject_id=int(row["id"]),
                rule="event-day-passed",
                action="dropped",
                reason=(
                    f'"{what}" was to be attended on {due.isoformat()}, which is over; '
                    "whether it happened is not something the ledger records"
                ),
            )
        )
    return out


def _canvas_assignments_past_grace(
    conn: sqlite3.Connection, today: date
) -> list[Disposal]:
    """Coursework whose due date passed long enough ago that the feed will never answer.

    `canvas.py` drops submitted and graded work before it is ever ingested, and that
    filter is the most valuable thing the Canvas API gives. The ICS fallback has none of
    it, so every assignment stays `open` after it is handed in — 141 of them arrived on
    2026-08-20 when the connector's cursor bug was fixed, and not one can ever close
    itself.

    So the owner ruled that time closes them, and `CANVAS_GRACE_DAYS` carries the reason.
    This is the module's only rule that closes a deliverable, which is why it is bounded
    by source rather than by the shape of the sentence: a mail asking for the same essay
    is still owed, and only the row that came from the feed with no submission state is
    disposed of here.

    If `CANVAS_TOKEN` is ever granted, `canvas.py` supersedes the feed, submission state
    returns, and this rule should be deleted rather than retuned.
    """
    horizon = today - timedelta(days=CANVAS_GRACE_DAYS)
    out: list[Disposal] = []
    for row in conn.execute(
        "SELECT c.id, c.what, c.due_at FROM commitment c"
        " JOIN source_item si ON si.id = c.source_item_id"
        " WHERE c.user_id = ? AND c.status = 'open' AND c.due_at IS NOT NULL"
        "   AND si.source = 'canvas:ics' ORDER BY c.id",
        (USER_ID,),
    ):
        try:
            due = date.fromisoformat(str(row["due_at"])[:10])
        except ValueError:
            continue
        if due >= horizon:
            continue
        out.append(
            Disposal(
                kind="commitment",
                subject_id=int(row["id"]),
                rule="canvas-past-grace",
                action="dropped",
                reason=(
                    f'"{row["what"]}" was due {due.isoformat()}, more than '
                    f"{CANVAS_GRACE_DAYS} days ago; the Canvas feed carries no "
                    "submission state and will never close it"
                ),
            )
        )
    return out


def _completed_reminders(conn: sqlite3.Connection) -> list[Disposal]:
    """The reminder was ticked upstream and the commitment behind it never heard.

    Found 2026-08-24 by asking why "Clean fishtank", due in January, was leading the
    owner's week. Apple Reminders emits a *second* item when an entry is completed —
    `x-apple-reminder://UUID:completed:2026-08-12T18:54:31.000Z` beside the original
    `x-apple-reminder://UUID` — and triage drops it, correctly: it carries no new
    obligation and it is not something to read. But nothing else looked at it either, so
    the commitment the original reminder created stayed open forever. Six completed
    reminders had been ingested, six dropped, and **five open commitments** were standing
    behind them, three of which led the state doc's "this week" section.

    This is positive contradiction, not silence: the row being closed is contradicted by
    another row in the same source, naming the same reminder, saying it was done. The
    join is on the UUID before the `:completed:` marker, so it cannot match by wording.

    `resolved`, not `dropped`. The owner ticked it off; they did the thing.
    """
    out: list[Disposal] = []
    for row in conn.execute(
        """
        SELECT c.id, c.what, done.external_id AS done_id, done.occurred_at AS done_at
          FROM commitment c
          JOIN source_item base ON base.id = c.source_item_id AND base.source = 'reminders'
          JOIN source_item done
            ON done.user_id = c.user_id AND done.source = 'reminders'
           AND done.external_id LIKE base.external_id || ':completed:%'
         WHERE c.user_id = ? AND c.status = 'open'
         ORDER BY c.id
        """,
        (USER_ID,),
    ):
        out.append(
            Disposal(
                kind="commitment",
                subject_id=int(row["id"]),
                rule="reminder-completed",
                action="resolved",
                reason=(
                    f'the reminder behind "{row["what"]}" was marked complete on '
                    f"{str(row['done_at'])[:10]} ({row['done_id']})"
                ),
            )
        )
    return out


def _evidence_was_retracted(conn: sqlite3.Connection) -> list[Disposal]:
    """The item this commitment was extracted from is gone from its source.

    `retraction.py` certifies that only from a *windowed complete read* — a source that
    can say "here is everything, and this is not in it" — so a retraction is a positive
    statement that the evidence no longer exists, never an inference from silence. A
    commitment whose only evidence has been retracted is standing on nothing.

    Dropped rather than resolved: nothing says the work happened, only that the thing
    that asked for it is gone. Tombstoned with the retraction cited, one status flip to
    reverse, exactly like every other disposal here.

    This is the rule that makes retraction mean something downstream. Without it the
    retraction table was a record nobody read.
    """
    out: list[Disposal] = []
    for row in conn.execute(
        """
        SELECT c.id, c.what, r.retracted_at, r.reason
          FROM commitment c
          JOIN source_item_retraction r
            ON r.source_item_id = c.source_item_id AND r.user_id = c.user_id
         WHERE c.user_id = ? AND c.status = 'open'
         ORDER BY c.id
        """,
        (USER_ID,),
    ):
        out.append(
            Disposal(
                kind="commitment",
                subject_id=int(row["id"]),
                rule="evidence-retracted",
                action="dropped",
                reason=(
                    f'the source item behind "{row["what"]}" was retracted on '
                    f"{str(row['retracted_at'])[:10]} — {row['reason']}"
                ),
            )
        )
    return out


def _upstream_due_dates_moved(conn: sqlite3.Connection) -> list[Disposal]:
    """Canvas moved the deadline and the ledger never noticed. Goal 4, increment B1.

    Five CIS 236 assignments moved upstream — 1-1-1 through 1-1-4 from 2026-08-23 to
    08-25, the Team Charter from 08-31 to 09-04 — and the commitments still held the old
    dates. The cause is structural rather than a missed case: `source_item` is immutable,
    so re-reading a changed assignment produces a `content_hash` conflict that is
    recorded and skipped, and **not one byte propagates**. A deadline extension was
    invisible to this system.

    The `assignment` table is the half that is allowed to change — a mirror of a live
    upstream record, refreshed by `coursework.upsert` on every feed read — so where its
    `due_at` disagrees with the commitment's, the upstream record is the one that is
    current and the ledger's is stale.

    This is a **move**, not a disposal, and it is the only rule in this file that changes
    a row instead of closing one. It is here anyway because it is the same shape as
    everything else: a positive contradiction between two rows the ledger already holds,
    with both values printable. It gets its own action so the Decisions page can tell a
    move from a close — a reader who cannot tell them apart would have no way to know
    whether the checker had been closing their coursework.

    Compared on the leading ten characters, for the reason every date comparison in this
    codebase is: `due_at` mixes bare dates, naive locals and offset-bearing datetimes,
    and only `substr(col, 1, 10)` means the same local day under all three.
    """
    out: list[Disposal] = []
    for row in conn.execute(
        """
        SELECT c.id, c.what,
               substr(c.due_at, 1, 10)  AS ledger_due,
               substr(a.due_at, 1, 10)  AS feed_due,
               a.last_changed_at        AS read_at
          FROM commitment c
          JOIN assignment a ON a.source_item_id = c.source_item_id AND a.user_id = c.user_id
         WHERE c.user_id = ? AND c.status = 'open'
           AND a.due_at IS NOT NULL AND c.due_at IS NOT NULL
           AND substr(a.due_at, 1, 10) != substr(c.due_at, 1, 10)
         ORDER BY c.id
        """,
        (USER_ID,),
    ):
        out.append(
            Disposal(
                kind="commitment",
                subject_id=int(row["id"]),
                rule="upstream-due-moved",
                action="moved",
                new_value=str(row["feed_due"]),
                reason=(
                    f'"{row["what"]}" was due {row["ledger_due"]}; the feed read on '
                    f"{str(row['read_at'])[:10]} says {row['feed_due']}"
                ),
            )
        )
    return out


def check(conn: sqlite3.Connection, settings: Settings, today: date) -> Report:
    """Every rule, each failing on its own. Read-only: `check` decides, `apply` writes."""
    report = Report()
    for rule in (
        lambda: _reported_done(conn),
        lambda: _completed_reminders(conn),
        lambda: _evidence_was_retracted(conn),
        lambda: _upstream_due_dates_moved(conn),
        lambda: _events_whose_day_has_passed(conn, today),
        lambda: _canvas_assignments_past_grace(conn, today),
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
                if disposal.action == "moved":
                    _move_due(conn, settings, disposal, note)
                elif disposal.action == "resolved":
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


def _move_due(
    conn: sqlite3.Connection, settings: Settings, disposal: Disposal, note: str
) -> None:
    """Write the upstream deadline onto the commitment, and knock.

    Three things, in this order, because the last one is best-effort and must not cost
    the first two: the row moves, the change is recorded in `claim_event` so anything
    reading a subject's history sees why its date changed, and the owner is told through
    notify's dedup key — a deadline that moves under a plan is exactly the class of
    change that must not be discovered by missing it.

    `WHERE status = 'open'` re-read at write time, like every other writer here: the row
    may have been closed between `check` and `apply`, and a closed commitment must not
    quietly acquire a new due date.
    """
    from backglass import claim_events

    old_due = conn.execute(
        "SELECT substr(due_at, 1, 10) AS d, what FROM commitment WHERE id = ? AND user_id = ?",
        (disposal.subject_id, USER_ID),
    ).fetchone()
    cur = conn.execute(
        "UPDATE commitment SET due_at = ?, resolution_note = ?"
        " WHERE id = ? AND user_id = ? AND status = 'open'",
        (disposal.new_value, note, disposal.subject_id, USER_ID),
    )
    if not cur.rowcount:
        return
    claim_events.record(
        conn,
        subject_table="commitment",
        subject_id=disposal.subject_id,
        cause="upstream_due_moved",
        field="due_at",
        old_value=str(old_due["d"]) if old_due else None,
        new_value=disposal.new_value,
    )
    try:
        from backglass import notify

        notify.record(
            conn,
            settings,
            kind="deadline-moved",
            subject_key=f"commitment:{disposal.subject_id}:{disposal.new_value}",
            title="A deadline moved",
            body=(
                f"{old_due['what'] if old_due else 'An assignment'} is now due "
                f"{disposal.new_value} (was {old_due['d'] if old_due else 'unset'})."
            ),
        )
    except Exception:  # noqa: BLE001 — rule 5: the move is done either way
        pass


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


def recent(conn: sqlite3.Connection, limit: int = 20) -> list[Any]:
    """What the checker disposed of lately, for the surface that has to show its work.

    The rows live in `decision` with everything else the ledger can justify; `decisions`
    owns the split between what the owner decided and what the machine tidied, so this is
    a name in the checker's own vocabulary for the same read.
    """
    from backglass import decisions

    return decisions.disposals(conn, limit=limit)
