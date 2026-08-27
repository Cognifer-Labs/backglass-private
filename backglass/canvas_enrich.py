"""What the Canvas calendar feed cannot say, taken from the owner's own browser session.

Owner's ask, 2026-08-27: *"continue to enrich processor."*

**The premise this started with was wrong, and the measurement is why it did not ship
that way.** The plan was to pull assignment descriptions off Canvas's REST API, on the
belief that the ICS feed carried thin text and 85 of the ledger's 206 assignments had no
description for `coursework.py` to read. Both halves were checked against the live ledger
before a line of it was built:

- Of 205 assignments matched to Canvas, the API's description was **longer on zero of
  them**. 164 were byte-identical and 41 were *shorter* than what the ledger already had,
  because `canvas_ics` keeps link URLs the API's HTML hides behind anchor text.
- Every assignment with no description in the ledger has none in Canvas either.
- Running `coursework.effort_for` over the API's text changed **0 of 205** estimates and
  0 of 8,221 total minutes.

So the feed is not thin. The enrichment worth having is the other thing the API knows,
and `connectors/canvas_ics.py` named it in its own docstring before any of this existed:

    "The feed carries no submission state. `canvas.py` drops anything already submitted
    or graded, and that filter is the most valuable thing the API gives; here it is
    absent, so work already handed in keeps reading as an open obligation."

Measured the same afternoon: **13 assignments Canvas had already graded were still open
commitments, holding 434 minutes of planner capacity**, one of them on that morning's
plan. The board was asking for seven hours of work that was finished.

**Why this is an import and not a connector, stated once so nobody improves it into one.**
ASU disables student-generated tokens — that is the documented reason `canvas_ics.py`
exists. The REST API does answer on the browser session in a signed-in tab, and turning
that into a scheduled connector is exactly what `canvas_ics.py` forswears: "There is no
session cookie, no scraping and no borrowed token here, and if the feed URL is revoked
this connector stops working, which is the correct behaviour." A sync job riding the
owner's cookie has none of those properties. So this reads a document the owner exported
from their own session at a moment they chose, and nothing here fetches anything.

`docs/07-connectors.md` §Canvas carries the snippet that produces the document.

**Two writes, and the second one is opt-in.** Recording what Canvas said is safe and
idempotent. *Closing a commitment* on the strength of it is a change to the ledger the
owner reads every morning, so it needs `close_submitted=True` and a `--dry-run` first —
the resolution note quotes Canvas, per rule 1, so a wrong close is legible rather than
mysterious.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backglass.db import now_iso
from backglass.ledger import USER_ID

#: Canvas submission `workflow_state` values that assert a submission by themselves.
#: `pending_review` is one: it is handed in and waiting on a human, which is no longer the
#: owner's problem. **`graded` is deliberately not here.** Canvas writes `graded` both for
#: work it took in and for work it scored zero because nothing arrived, and telling those
#: apart needs `submitted_at` or a mark above zero — see `Record.finished`.
_SUBMITTED_STATES = frozenset({"submitted", "pending_review", "complete"})


@dataclass(frozen=True)
class Record:
    """One assignment as the browser export describes it.

    Only the fields this writes plus the two it decides with. The export carries more —
    rubrics, attempt limits, submission types — and they are deliberately not stored:
    a column nothing reads is a column that goes stale without anyone noticing.
    """

    canvas_id: int
    points_possible: float | None
    submitted_at: str | None
    submission_state: str | None
    score: float | None
    title: str = ""

    @property
    def external_id(self) -> str:
        """The join key, and the only one. `canvas_ics` writes `assignment:<canvas id>`
        as the row's `external_id`, so the two sides agree on an integer the institution
        assigned. Title matching was never on the table: two courses run "Journal 2" in
        the same week."""
        return f"assignment:{self.canvas_id}"

    @property
    def finished(self) -> bool:
        """Whether Canvas says the work was actually handed in.

        A submission event settles it outright — including a submission that scored zero,
        which is finished work that went badly and not work still to do.

        Without one, a mark counts only if it is above zero. Autograded activities record
        a score and no submission event, and a rule demanding a timestamp would leave
        every one of them open. But **Canvas also writes a zero for work that was never
        submitted**, and the live export proved the difference matters: "Module 1
        Scientific Reasoning Homework 1" came back `graded, 0/6` with no `submitted_at`
        and a due date three days in the future. Closing that would have told the owner
        they had done something they had not, in the one place they go to find out.
        """
        if self.submitted_at:
            return True
        if str(self.submission_state or "") in _SUBMITTED_STATES:
            return True
        return self.score is not None and self.score > 0


@dataclass
class EnrichReport:
    """What the import did, in the terms the owner checks it in."""

    matched: int = 0
    #: Rows in the export with no assignment behind them. Almost always real and almost
    #: always fine: Canvas lists 321 assignments and the dated feed carries 206, so an
    #: undated practice item is unmatched by construction. Counted, never inserted —
    #: creating assignment rows is the connector's job and this is not a second connector.
    unmatched: int = 0
    updated: int = 0
    unchanged: int = 0
    #: commitment_id -> the sentence that closed it. Empty unless `close_submitted`.
    closed: dict[int, str] = field(default_factory=dict)
    #: Work Canvas says is done whose commitment this run did not close, either because
    #: closing was off or because the row was already resolved. Named rather than
    #: counted: it is the list the owner decides on.
    finished_still_open: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def load(path: Path) -> list[Record]:
    """Read the browser export.

    Tolerant on the way in and strict about what it keeps: the export is a document a
    person produced by hand in a browser console, so a row that is not an assignment (an
    error marker the snippet appends when a course returns 403) is skipped rather than
    raising. A file that is not a JSON array at all is a different mistake and does raise
    — that one is worth stopping for.
    """
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a JSON array of assignments")
    out: list[Record] = []
    for entry in raw:
        if not isinstance(entry, dict) or entry.get("canvas_id") is None:
            continue
        out.append(
            Record(
                canvas_id=int(entry["canvas_id"]),
                points_possible=_number(entry.get("points_possible")),
                submitted_at=_text(entry.get("submitted_at")),
                submission_state=_text(entry.get("submission_workflow_state")),
                score=_number(entry.get("score")),
                title=str(entry.get("name") or ""),
            )
        )
    return out


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def apply(
    conn: sqlite3.Connection,
    records: list[Record],
    *,
    source: str = "canvas:ics",
    close_submitted: bool = False,
    dry_run: bool = False,
) -> EnrichReport:
    """Write what Canvas said onto the assignments that already exist.

    Rule 3, and it is the property this stands or falls on: the same export applied twice
    writes nothing the second time. `enriched_at` is therefore only stamped on a row whose
    values actually moved — a column bumped on every import would make every re-run a
    write for all two hundred rows and quietly destroy the guarantee.

    Never inserts. An export row with no assignment behind it is counted and skipped; see
    `EnrichReport.unmatched`.
    """
    report = EnrichReport()
    stamp = now_iso()

    for record in records:
        row = conn.execute(
            "SELECT * FROM assignment WHERE user_id = ? AND source = ? AND external_id = ?",
            (USER_ID, source, record.external_id),
        ).fetchone()
        if row is None:
            report.unmatched += 1
            continue
        report.matched += 1

        changed = {
            "points_possible": record.points_possible,
            "submitted_at": record.submitted_at,
            "submission_state": record.submission_state,
            "score": record.score,
        }
        diff = {k: v for k, v in changed.items() if row[k] != v}
        if diff:
            report.updated += 1
            if not dry_run:
                assignments_sql = ", ".join(f"{column} = ?" for column in diff)
                conn.execute(
                    f"UPDATE assignment SET {assignments_sql}, enriched_at = ? WHERE id = ?",
                    (*diff.values(), stamp, int(row["id"])),
                )
        else:
            report.unchanged += 1

        if not record.finished:
            continue
        _settle(conn, row, record, report, close_submitted=close_submitted, dry_run=dry_run)

    return report


def _settle(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    record: Record,
    report: EnrichReport,
    *,
    close_submitted: bool,
    dry_run: bool,
) -> None:
    """Close the commitments behind an assignment Canvas has already taken in.

    The commitment is reached through `source_item_id` rather than by matching titles,
    because that is the link `coursework.apply_estimates` already trusts to push an
    estimate onto the row the planner reads — one join, used the same way in both
    directions.

    `actions.resolve` rather than an UPDATE of its own: it is the one place that writes
    `status`, `resolved_at` and the `claim_event` together, and a second writer of those
    three would be a second definition of what "done" means.
    """
    from backglass.web import actions

    if row["source_item_id"] is None:
        return
    commitments = conn.execute(
        "SELECT id, what, status FROM commitment "
        "WHERE user_id = ? AND source_item_id = ? AND status = 'open'",
        (USER_ID, int(row["source_item_id"])),
    ).fetchall()
    if not commitments:
        return

    note = _note(record)
    for commitment in commitments:
        title = f"{str(commitment['what'])[:60]} — {note}"
        if not close_submitted:
            report.finished_still_open.append(title)
            continue
        report.closed[int(commitment["id"])] = title
        if dry_run:
            continue
        actions.resolve(conn, int(commitment["id"]), note)


def _note(record: Record) -> str:
    """Rule 1: the claim carries where it came from.

    "canvas:" prefixed the way `logic.py` prefixes its own resolutions, so a note written
    by an import is distinguishable from one the owner typed — by a person reading the
    row and by a query.
    """
    parts = [f"canvas: {record.submission_state or 'submitted'}"]
    if record.submitted_at:
        parts.append(f"submitted {record.submitted_at[:10]}")
    if record.score is not None:
        total = "" if record.points_possible is None else f"/{_plain(record.points_possible)}"
        parts.append(f"scored {_plain(record.score)}{total}")
    return ", ".join(parts)


def _plain(value: float) -> str:
    """`5` rather than `5.0`, and `1.83` rather than `1.833333333333333` — both shapes
    came back from the live export and neither reads as a mark in a sentence."""
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")
