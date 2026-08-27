"""The whole neglected board at once, so it can be cleared in one sitting.

`questions.py` asks well and asks slowly. `STALE_BATCH_LIMIT = 5` exists for a good
reason — *"a wall of questions is how an owner stops answering questions"* — and against a
board that grows more slowly than the owner answers, five a day converges.

The owner's board is not that board. On 2026-08-20 it held 366 open commitments, 64 of
them past due, the oldest a fishtank due in January, and 23 questions already waiting
unanswered. Five a day against a queue nobody is drawing down is not a drip, it is a
standstill; the arithmetic never reaches the fishtank. The owner's words were *"it is
planning for things that are obviously done"*, and every one of those rows was still
eligible for tomorrow's plan.

So this is the other surface, and the difference is deliberate:

- **Ask** shows one question, full screen, and wants a considered answer.
- **Scrub** shows everything disposable at once and wants a fast pass. Nothing here is
  a question — every row is a commitment with two obvious buttons and a reason it is on
  the list.

It **detects, it never disposes.** `logic.py` closes what the record contradicts and this
module closes nothing at all: every row here is one the machine specifically could *not*
justify closing on its own, which is exactly why it needs a person. That split is the
whole reason both files can be trusted — a detector that also acted would have no reason
to be conservative.

Rows are grouped by *why they are here*, because the reason changes the answer. "Due four
months ago and never mentioned again" is usually done or dead. "The same obligation, and
you already closed its twin" is almost always a duplicate. Presenting them in one
undifferentiated list would make the owner rediscover the reason for every row.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

from backglass import staleness
from backglass.ledger import USER_ID

#: Past due by more than this and still open, without qualifying as stale, lands in the
#: overdue group. A week, because the working assumption behind a shorter window — that
#: the owner simply has not got to it yet — stops being reasonable after one.
OVERDUE_DAYS = 7


@dataclass(frozen=True)
class Row:
    """One commitment on the scrub board, with the reason it is here."""

    id: int
    what: str
    due_at: str | None
    reason: str
    source: str | None
    days_overdue: int | None


@dataclass(frozen=True)
class Group:
    key: str
    title: str
    #: What this group means, shown once above the rows rather than repeated on each.
    blurb: str
    rows: list[Row]


def _days_overdue(due_at: str | None, today: date) -> int | None:
    if not due_at:
        return None
    try:
        return (today - date.fromisoformat(str(due_at)[:10])).days
    except ValueError:
        return None


def _stale(conn: sqlite3.Connection, today: date) -> list[Row]:
    """The same rows `questions.py` drips out at five a day, all of them, unasked.

    No `limit`. The batch cap is a property of the question surface — of how many things a
    person should be asked in one sitting — and this surface's entire premise is that the
    owner came here to see the backlog.
    """
    out: list[Row] = []
    for row in staleness.stale_rows(conn, today):
        # `_STALE_SQL` projects the due date as `due_day`, already truncated, and carries
        # the last-mention source alongside it — that is the evidence the row is built on,
        # so it is what the owner is shown.
        due = str(row["due_day"]) if row["due_day"] else None
        seen = str(row["seen_day"]) if row["seen_day"] else None
        out.append(
            Row(
                id=int(row["id"]),
                what=str(row["what"]),
                due_at=due,
                reason=(
                    f"due {due} and nothing has mentioned it since {seen}"
                    if due and seen
                    else "long past due, and nothing has mentioned it since"
                ),
                source=str(row["source"]) if row["source"] else None,
                days_overdue=_days_overdue(due, today),
            )
        )
    return out


def _overdue(
    conn: sqlite3.Connection, today: date, exclude: set[int]
) -> list[Row]:
    """Past due, but recently enough or noisily enough that `staleness` will not touch it.

    `staleness` needs both an old due date and silence since. A commitment mentioned last
    week and due last month satisfies only the first, so it never becomes stale and never
    becomes a question — it simply stays on the board being planned. This is the group
    that catches those.
    """
    floor = date.fromordinal(today.toordinal() - OVERDUE_DAYS).isoformat()
    out: list[Row] = []
    for row in conn.execute(
        "SELECT c.id, c.what, c.due_at, si.source FROM commitment c"
        " LEFT JOIN source_item si ON si.id = c.source_item_id"
        " WHERE c.user_id = ? AND c.status = 'open' AND c.due_at IS NOT NULL"
        "   AND substr(c.due_at, 1, 10) < ?"
        " ORDER BY c.due_at",
        (USER_ID, floor),
    ):
        if int(row["id"]) in exclude:
            continue
        days = _days_overdue(row["due_at"], today)
        out.append(
            Row(
                id=int(row["id"]),
                what=str(row["what"]),
                due_at=str(row["due_at"]),
                reason=f"{days} days past due and still open" if days else "past due",
                source=str(row["source"]) if row["source"] else None,
                days_overdue=days,
            )
        )
    return out


def _resurrected(conn: sqlite3.Connection, exclude: set[int]) -> list[Row]:
    """Open rows extracted from a source item whose obligation the owner already closed.

    Commitment 92 on the live ledger is `communicate housing issue to ASU University
    Housing`, resolved by the owner. 213 and 304 are the same sentence out of the same
    mail, extracted again on later passes, both open. `actions.drop` tombstones for
    exactly this reason and `resolve` does not, so closing one as *done* leaves the door
    open for the next extraction pass to walk back through it.

    Matched on the shared `source_item_id` rather than on the words, because the words
    drift between prompt versions — those three rows are three paraphrases — while the
    item they came from cannot.
    """
    out: list[Row] = []
    for row in conn.execute(
        "SELECT c.id, c.what, c.due_at, si.source, ("
        "  SELECT GROUP_CONCAT(o.status) FROM commitment o"
        "  WHERE o.source_item_id = c.source_item_id AND o.id != c.id"
        "    AND o.status IN ('done', 'dropped')"
        ") AS closed_siblings"
        " FROM commitment c JOIN source_item si ON si.id = c.source_item_id"
        " WHERE c.user_id = ? AND c.status = 'open'"
        " ORDER BY c.id",
        (USER_ID,),
    ):
        if not row["closed_siblings"] or int(row["id"]) in exclude:
            continue
        statuses = sorted(set(str(row["closed_siblings"]).split(",")))
        out.append(
            Row(
                id=int(row["id"]),
                what=str(row["what"]),
                due_at=str(row["due_at"]) if row["due_at"] else None,
                reason=(
                    "you already closed another commitment from this same source "
                    f"({'/'.join(statuses)})"
                ),
                source=str(row["source"]) if row["source"] else None,
                days_overdue=None,
            )
        )
    return out


#: How alike two plans must read before they are worth showing as a suspected pair. Below
#: `dedup_threshold` on purpose — at or above it the matcher would already have merged them
#: at ingest, so the band this surface exists for is exactly the one the matcher refuses.
#: 0.6 is `dedup.SUSPECT_FLOOR`, the same floor the commitment board uses.
PLAN_SUSPECT_FLOOR = 0.6

#: Most-alike first, and capped: this is a fast pass, not an audit. The count above the
#: rows says how many were left, so the cap is never silent.
PLAN_PAIR_LIMIT = 30


@dataclass(frozen=True)
class PlanPair:
    """Two plans that look like one, and what the owner needs to tell them apart."""

    a_id: int
    a_what: str
    a_when: str | None
    b_id: int
    b_what: str
    b_when: str | None
    score: float
    day: str


def duplicate_plans(
    conn: sqlite3.Connection, settings: Any, *, limit: int = PLAN_PAIR_LIMIT
) -> tuple[list[PlanPair], int]:
    """Same-day plans that read alike, minus the ones the owner has already separated.

    Returns (pairs, total) so the surface can say what the cap left out.

    **This exists because of a trade-off, not in spite of one.**
    `extract/engagements._same_row` refuses to merge two sightings whose wording differs,
    and its docstring carries four rounds of verification behind the reason: a duplicate
    is visible and dismissable, while a wrong merge silently replaces a plan the owner
    already agreed to. Correct — but measured on 2026-08-24 the ledger held 127 same-day
    near-duplicate plans, 120 of them below the wording threshold, and there was nowhere
    to dismiss any of them. The trade-off was being paid and its compensation had never
    been built.

    So this detects and never disposes, like everything else in this module. Same day is
    required rather than scored: two plans a week apart are two plans however alike the
    words, and a standing arrangement (pickleball every Tuesday) must not collapse into
    one row — which is the failure mode the engagement matcher already warns about.
    """
    from backglass.extract import entities

    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT id, what, starts_at, substr(starts_at, 1, 10) AS day FROM engagement"
            " WHERE user_id = ? AND status IN ('proposed', 'confirmed')"
            "   AND starts_at IS NOT NULL"
            " ORDER BY starts_at, id",
            (USER_ID,),
        )
    ]
    settled = {
        (int(r["low_id"]), int(r["high_id"]))
        for r in conn.execute(
            "SELECT low_id, high_id FROM engagement_distinct WHERE user_id = ?",
            (USER_ID,),
        )
    }

    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_day.setdefault(str(row["day"]), []).append(row)

    pairs: list[PlanPair] = []
    for day_key, same_day in by_day.items():
        for index, first in enumerate(same_day):
            for second in same_day[index + 1:]:
                # Ordered by id, so the pair reads the same way it is stored, acted on
                # and remembered — `same_plan` keeps the lower id and `engagement_distinct`
                # normalises low<high, and a surface that showed them the other way round
                # would label the wrong row "the one you keep".
                lo, hi = sorted((first, second), key=lambda row: int(row["id"]))
                if (int(lo["id"]), int(hi["id"])) in settled:
                    continue
                score = entities.similar(str(lo["what"]), str(hi["what"]))
                if score < PLAN_SUSPECT_FLOOR:
                    continue
                pairs.append(
                    PlanPair(
                        a_id=int(lo["id"]),
                        a_what=str(lo["what"]),
                        a_when=_clock(lo),
                        b_id=int(hi["id"]),
                        b_what=str(hi["what"]),
                        b_when=_clock(hi),
                        score=round(score, 2),
                        day=day_key,
                    )
                )
    pairs.sort(key=lambda pair: (-pair.score, pair.day, pair.a_id))
    return pairs[:limit], len(pairs)


def _clock(row: dict[str, Any]) -> str | None:
    """The hour a plan names, or None when it names only a day.

    Sliced rather than parsed: `starts_at` holds three shapes in this ledger — a bare
    date, a naive local time, and an offset-bearing one — and the surface's job is to show
    the owner what the row says, not to resolve it into an instant. A pair whose two rows
    disagree only in shape is exactly the pair the owner is being asked to look at.
    """
    value = str(row["starts_at"] or "")
    return value[11:16] if len(value) >= 16 else None


def board(conn: sqlite3.Connection, today: date) -> list[Group]:
    """Every group with rows in it, most-disposable first.

    Groups are built in precedence order and each excludes the ids already claimed, so no
    commitment appears twice — a row offered in two places is a row the owner acts on
    twice, and the second click is an error on a closed commitment.
    """
    claimed: set[int] = set()
    groups: list[Group] = []

    for key, title, blurb, rows in (
        (
            "stale",
            "Gone quiet",
            "Long past due, and nothing in mail, messages or notes has mentioned them "
            "since. Usually these are done, or they died quietly.",
            _stale(conn, today),
        ),
        (
            "resurrected",
            "Already handled once",
            "You closed one commitment from each of these source items and extraction "
            "made another. Almost always a duplicate of something finished.",
            _resurrected(conn, claimed),
        ),
        (
            "overdue",
            "Past due",
            "Their date has gone by. Backglass has no evidence either way, so it has "
            "kept planning them.",
            _overdue(conn, today, claimed),
        ),
    ):
        fresh = [row for row in rows if row.id not in claimed]
        claimed.update(row.id for row in fresh)
        if fresh:
            groups.append(Group(key=key, title=title, blurb=blurb, rows=fresh))
    return groups


def counts(conn: sqlite3.Connection, today: date) -> dict[str, int]:
    """How much is waiting, for a caller that wants the number without the rows."""
    groups = board(conn, today)
    out: dict[str, Any] = {group.key: len(group.rows) for group in groups}
    out["total"] = sum(len(group.rows) for group in groups)
    return out
