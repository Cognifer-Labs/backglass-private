"""Which day each piece of work is actually getting, and what will not fit before it is due.

Owner's ask, 2026-08-27: "make the scheduler better it should allocate time to every
homework after estimating completing it and other events and stuff."

The estimating half was already done. `coursework.py` reads real numbers off the
assignments — on the live ledger, 172 open obligations carry `estimate_source =
'analyzed'` for 121 hours of work — and `analyzed` is also what makes a candidate
divisible, so a 344-minute milestone already arrives as sittings rather than as one
unschedulable lump.

The allocating half did not exist. **The planner's horizon is one day.** `candidates()`
sorts everything not due this week into `PRIORITY_REST`, `select` spends today's budget,
and whatever is left is "overflow" — counted and forgotten. The owner's real plan this
morning ended with *216 item(s) did not fit*, a number that names no day, no assignment
and no consequence. An assignment due in three weeks is invisible every morning until the
week it is due, and then it competes for a Thursday with 145 free minutes against
everything else that waited the same way.

So this module answers the two questions the system could not: **which day is this getting,
and what cannot finish in time?**

**A forecast, not a plan.** Nothing here writes a `day_plan` for a future day. Those would
fight `proposed`/`accepted`/`superseded`, the `--if-missing` catch-up net, and docs/04 §6's
"proposed, never imposed" — and a plan for next Tuesday made from today's knowledge is
wrong by Tuesday. `propose()` remains the only thing that writes a day; this is advice it
reads on the way in.

**Nothing is stored, either.** An allocation table would need a migration, a staleness
story, and a fingerprint to keep CLAUDE.md rule 3 (two runs, no upstream change, zero
writes). Recomputing a fortnight of capacity is a handful of reads and carries none of it.

**Earliest deadline first, packed as early as it fits.** Frontloaded rather than
just-in-time, for two reasons: it agrees with the owner's own ruling that a weekday should
hold about two hours of homework, and slack in front of a deadline is most of the value of
having planned it at all.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from backglass.config import Settings
from backglass.plan import capacity as capacity_mod

if TYPE_CHECKING:  # `planner` imports this module; the dependency only runs one way.
    from backglass.plan.planner import Candidate

#: How far forward to look. A setting rather than a constant would be better if anything
#: else wanted it; until something does, the number lives here with its reasoning.
#:
#: Fourteen days, bounded from both sides. Past the Apple Calendar sync window the only
#: fixed events on a future day are the `calendar:asu` class rows, so capacity out there
#: reads optimistically — every meeting and dinner that will exist by then is missing, and
#: a forecast that promised a free Tuesday three months out would be arithmetic rather
#: than advice. Fourteen is far enough to see a fortnight of coursework and near enough
#: that the capacity behind it is mostly real.
DEFAULT_HORIZON_DAYS = 14


@dataclass(frozen=True)
class Sitting:
    """One piece of one obligation, on one day."""

    day: date
    minutes: int


@dataclass(frozen=True)
class Unplaced:
    """Work the fortnight had no room for, and how much of it is left over.

    Not a failure — `unreachable` is the failure. This is the ordinary consequence of a
    bounded horizon: 202 hours of dated work against a fortnight holding 92 of them means
    two thirds of the board gets nothing, and its deadlines are far enough out that
    nothing has gone wrong yet. It is here because the alternative was silence: an
    obligation with no sittings and a deadline past `horizon_end` used to leave no trace
    on any surface, so the runway looked like the whole board when it was the half of it
    that fitted. P2's rule — overflow is stated, never silently truncated — applies to a
    forecast exactly as it applies to a day.
    """

    item: Candidate
    #: Minutes still unallocated when the horizon ran out. The whole estimate for work
    #: that got nothing, the remainder for work that got a sitting or two.
    minutes: int


@dataclass
class Runway:
    """What the next fortnight can hold, per obligation.

    `sittings` is the whole answer; the other two fields are the questions a caller
    actually asks, precomputed so every caller asks them the same way.
    """

    start: date
    #: commitment_id -> the days it was given, in order.
    sittings: dict[int, list[Sitting]] = field(default_factory=dict)
    #: Work whose remaining minutes do not fit before its due date at this capacity.
    #: The sentence worth waking up to, and the reason this module returns something
    #: other than a schedule.
    unreachable: list[Candidate] = field(default_factory=list)
    #: Dated work whose deadline is past the horizon and which the horizon had no minutes
    #: left for. Deadline order, and stated rather than dropped — see `Unplaced`.
    beyond: list[Unplaced] = field(default_factory=list)
    #: Days the horizon could not read at all — a `capacity.compute` that raised. Rule 5:
    #: a day that cannot be measured costs its own allocation and nothing else, and it is
    #: named rather than silently treated as full.
    unreadable: list[date] = field(default_factory=list)

    def on(self, day: date) -> set[int]:
        """The commitment ids this runway puts on `day`."""
        return {
            cid
            for cid, sittings in self.sittings.items()
            if any(s.day == day for s in sittings)
        }

    def finishes(self, commitment_id: int) -> date | None:
        """The day the last sitting lands, or None if it never does."""
        sittings = self.sittings.get(commitment_id)
        return sittings[-1].day if sittings else None


def allocate(
    conn: sqlite3.Connection,
    settings: Settings,
    items: list[Candidate],
    start: date,
    *,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    not_before: datetime | None = None,
) -> Runway:
    """Lay every candidate's remaining work onto the days before it is due.

    `items` are the planner's own candidates, passed in rather than re-read, so the
    runway and the day it advises can never disagree about what is open, what is stale or
    what has already been done. `not_before` clamps the *first* day only — the same clamp
    `capacity.compute` takes, so a run at four in the afternoon allocates the afternoon
    and not the morning it has already spent.

    Earliest deadline first. Undated work is allocated last and only into whatever the
    dated work left, which is the honest ordering: a thing with no deadline cannot
    displace a thing with one, and it is also the band the planner already calls
    `PRIORITY_REST`.
    """
    horizon = _horizon(conn, settings, start, horizon_days, not_before)
    runway = Runway(
        start=start, unreadable=[d for d, cap in horizon.items() if cap is None]
    )

    # A day under the P3 floor gets zero rather than its minutes: `propose` will decline
    # to plan it, so promising an assignment a day the planner is going to refuse would
    # be a forecast of something that cannot happen.
    remaining: dict[date, int] = {
        day: (cap.capacity_minutes if cap.plannable else 0)
        for day, cap in horizon.items()
        if cap is not None
    }
    largest: dict[date, int] = {
        day: (cap.largest_slot if cap.plannable else 0)
        for day, cap in horizon.items()
        if cap is not None
    }
    placed: dict[int, list[Sitting]] = defaultdict(list)

    horizon_end = max(remaining) if remaining else start

    for item in _earliest_deadline_first(items, start):
        left = item.remaining
        deadline = _deadline(item)
        # An overdue obligation has no deadline left to beat — it is late, and the only
        # useful answer is "as early as there is room". Cutting it off at a date in the
        # past instead gave it *nothing*: every day of the horizon is after it, so the
        # first draft allocated zero minutes to the five most urgent things on the
        # owner's board and then reported them as impossible. Found on the live ledger,
        # not in a fixture.
        overdue = deadline is not None and deadline < start
        cutoff = None if overdue else deadline
        for day in sorted(remaining):
            if left <= 0:
                break
            # Work is allocated up to and including the day it is due. Past that the day
            # is not a place this can happen, whatever room it has.
            if cutoff is not None and day > cutoff:
                break
            room = remaining[day]
            if room <= 0:
                continue
            piece = min(item.sitting(settings), left, room)
            if not item.divisible:
                # `select`'s rule, mirrored rather than approximated: indivisible work
                # needs one contiguous run, so a day whose largest hole is smaller than
                # the whole thing is not a day it can happen on. Without this the
                # forecast promises days the planner will refuse, which is worse than no
                # forecast — it is a wrong one that looks right.
                if item.remaining > min(room, largest.get(day, 0)):
                    continue
                piece = item.remaining
            if piece <= 0:
                continue
            placed[item.commitment_id].append(Sitting(day=day, minutes=piece))
            remaining[day] = room - piece
            left -= piece
        if left > 0 and deadline is not None and deadline > horizon_end:
            # Nothing has failed here — the fortnight simply stopped before this one's
            # deadline, or stopped having minutes. Recorded rather than dropped, because
            # the runway is read as "what is coming", and 89 of the owner's 204 Canvas
            # assignments were leaving no trace on it at all.
            runway.beyond.append(Unplaced(item=item, minutes=left))
        elif left > 0 and deadline is not None and deadline <= horizon_end:
            # Not "did not fit today" — did not fit *at all*, before the date it is owed.
            # This is the only thing in the planner that looks forward far enough to say
            # so, and saying it two weeks early is the entire point.
            #
            # Both guards are load-bearing, and the live ledger supplied both. Work due
            # *after* the horizon has not failed to fit — the fortnight simply stopped
            # before its deadline, and calling a November exam impossible in August is a
            # false alarm that would teach the owner to ignore the true ones. Undated
            # work cannot miss a date at all; it is `PRIORITY_REST` and it waits.
            runway.unreachable.append(item)

    runway.sittings = dict(placed)
    return runway


def _earliest_deadline_first(items: list[Candidate], start: date) -> list[Candidate]:
    """EDF, with the undated work behind all of it.

    This ordering is also the fix for a scar. On 2026-08-24 the day's first ninety
    minutes went to a CIS 236 RFP due 13 November while seven assignments due 28 August
    waited behind it, because `rollover_count` outranked every deadline. Under EDF that is
    unrepresentable: 28 August is allocated before 13 November, whatever either has done
    before. `order()` still ranks the day itself — this ranks the fortnight.
    """
    dated = [c for c in items if _deadline(c) is not None]
    undated = [c for c in items if _deadline(c) is None]
    # `commitment_id` as the final key so the walk is deterministic: two obligations due
    # the same day with the same estimate must allocate in the same order on every run,
    # or rule 3's "two runs, zero writes" fails on a coin toss.
    dated.sort(key=lambda c: (_deadline(c), -c.priority, c.commitment_id))  # type: ignore[arg-type,return-value]
    undated.sort(key=lambda c: (c.priority, -c.age_days, c.commitment_id))
    return dated + undated


def _deadline(item: Candidate) -> date | None:
    if not item.due_at:
        return None
    try:
        return date.fromisoformat(str(item.due_at)[:10])
    except ValueError:
        # `due_at` is TEXT with no CHECK behind it. A row that is not a date is undated
        # here rather than an exception that takes the whole forecast down — rule 5's
        # unit is the obligation, not the fortnight.
        return None


def _horizon(
    conn: sqlite3.Connection,
    settings: Settings,
    start: date,
    horizon_days: int,
    not_before: datetime | None,
) -> dict[date, capacity_mod.Capacity | None]:
    """Each day of the horizon, computed once.

    Once, deliberately: the walk needs both a day's total free minutes and its largest
    contiguous hole, and reading capacity twice per day would double the horizon's cost
    for two fields of one object.
    """
    return {
        start
        + timedelta(days=offset): _capacity(
            conn, settings, start + timedelta(days=offset),
            not_before if offset == 0 else None,
        )
        for offset in range(max(1, horizon_days))
    }


def _capacity(
    conn: sqlite3.Connection,
    settings: Settings,
    day: date,
    not_before: datetime | None,
) -> capacity_mod.Capacity | None:
    """One day's capacity, or None if it could not be computed.

    Each day resolves its own timezone inside `capacity.compute` — the owner moves
    between UTC-7 and UTC+5:30, and a fortnight computed in one zone would drift a day
    across the move. Nothing here does its own clock arithmetic for that reason.
    """
    try:
        return capacity_mod.compute(conn, settings, day, not_before=not_before)
    except Exception:  # noqa: BLE001 — rule 5: a day that will not read costs itself only
        return None
