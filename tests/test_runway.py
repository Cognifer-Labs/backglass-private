"""The fortnight, not the morning. `plan/runway.py`.

Owner's ask, 2026-08-27: "make the scheduler better it should allocate time to every
homework after estimating completing it and other events and stuff."

The planner's horizon was one day. Everything not due this week sorted into
`PRIORITY_REST`, `select` spent today's budget, and the rest was "overflow" — the owner's
real plan that morning ended with *216 item(s) did not fit*, a number that names no day,
no assignment and no consequence. These tests are about the two things the horizon can
say and one day cannot: which day a piece of work is getting, and what will not finish
before it is owed.

Two of them are here because the live ledger found the bug and a fixture did not; both
are marked.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from backglass.config import Settings
from backglass.plan import planner, runway
from tests.test_planner import PHOENIX, add_commitment

#: A Monday, so a full working week sits in front of every case here and no result
#: depends on a weekend rule doing the work by accident.
MONDAY = date(2026, 8, 31)


@pytest.fixture
def sett(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"default_tz": PHOENIX, "confidence_threshold": 0.7}
    )


def pool(conn, sett: Settings, day: date = MONDAY):  # type: ignore[no-untyped-def]
    return planner.candidates(conn, sett, day, set())


def days_for(plan: runway.Runway, commitment_id: int) -> list[date]:
    return [s.day for s in plan.sittings.get(commitment_id, [])]


# ── allocation ────────────────────────────────────────────────────────────


def test_every_dated_obligation_gets_a_day(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """The ask, at its plainest. Work that fits somewhere in the fortnight is told where."""
    ids = [
        add_commitment(conn, sett, f"assignment {i}", minutes=60,
                       due=MONDAY + timedelta(days=i + 1), n=i)
        for i in range(5)
    ]

    plan = runway.allocate(conn, sett, pool(conn, sett), MONDAY)

    for cid in ids:
        assert days_for(plan, cid), f"commitment {cid} was allocated no day at all"


def test_nothing_is_allocated_past_its_deadline(conn, sett) -> None:  # type: ignore[no-untyped-def]
    cid = add_commitment(conn, sett, "quiz", minutes=45, due=MONDAY + timedelta(days=2))

    plan = runway.allocate(conn, sett, pool(conn, sett), MONDAY)

    assert days_for(plan, cid)
    assert max(days_for(plan, cid)) <= MONDAY + timedelta(days=2)


def test_the_earliest_deadline_is_served_first(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """EDF, and the fix for a scar.

    On 2026-08-24 the day's first ninety minutes went to a CIS 236 RFP due 13 November
    while seven assignments due 28 August waited behind it, because `rollover_count`
    outranked every deadline and a key that never expires is not a tiebreak. Under EDF
    that is unrepresentable — which is the point of testing the ordering rather than the
    outcome of one day.
    """
    soon = add_commitment(conn, sett, "due Tuesday", minutes=120,
                          due=MONDAY + timedelta(days=1), n=1, rollovers=0)
    later = add_commitment(conn, sett, "RFP due in six weeks", minutes=120,
                           due=MONDAY + timedelta(days=42), n=2, rollovers=9)

    plan = runway.allocate(conn, sett, pool(conn, sett), MONDAY)

    assert min(days_for(plan, soon)) <= min(days_for(plan, later)), (
        "a November deadline took a day ahead of a Tuesday one — the 2026-08-24 bug"
    )


def test_work_is_frontloaded_rather_than_left_to_the_deadline(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """The owner's own two-hours-a-weekday ruling, and slack in front of a deadline is
    most of the value of having planned it."""
    cid = add_commitment(conn, sett, "essay", minutes=60, due=MONDAY + timedelta(days=10))

    plan = runway.allocate(conn, sett, pool(conn, sett), MONDAY)

    assert days_for(plan, cid) == [MONDAY]


def test_a_long_assignment_is_spread_across_days(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """`analyzed` is what makes a candidate divisible — an estimate `coursework.py` read
    off the assignment, which is a deliverable measured in pages or chapters and split by
    construction. Handed over whole it would be unschedulable on every day of its life."""
    cid = add_commitment(
        conn, sett, "CIS 236 milestone", minutes=340,
        due=MONDAY + timedelta(days=7), estimate_source="analyzed",
    )

    plan = runway.allocate(conn, sett, pool(conn, sett), MONDAY)
    got = days_for(plan, cid)

    assert len(got) > 1, "a 340-minute deliverable was promised to one day"
    assert sum(s.minutes for s in plan.sittings[cid]) == 340
    assert all(
        s.minutes <= sett.max_block_minutes for s in plan.sittings[cid]
    ), "a sitting ran past the block ceiling"


def test_undated_work_never_displaces_a_deadline(conn, sett) -> None:  # type: ignore[no-untyped-def]
    dated = add_commitment(conn, sett, "due Wednesday", minutes=90,
                           due=MONDAY + timedelta(days=2), n=1)
    add_commitment(conn, sett, "someday, tidy the notes", minutes=90, due=None, n=2)

    plan = runway.allocate(conn, sett, pool(conn, sett), MONDAY)

    assert days_for(plan, dated), "dated work lost its day to undated work"


# ── what will not fit, said early ─────────────────────────────────────────


def test_work_that_cannot_finish_in_time_is_reported(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """The sentence worth waking up to, and the reason this module returns something
    other than a schedule. Not dropped, not counted into an anonymous pile — named."""
    cid = add_commitment(conn, sett, "impossible essay", minutes=4_000,
                         due=MONDAY + timedelta(days=2), estimate_source="analyzed")

    plan = runway.allocate(conn, sett, pool(conn, sett), MONDAY)

    assert [c.commitment_id for c in plan.unreachable] == [cid]


def test_a_deadline_past_the_horizon_is_not_called_impossible(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """Found on the live ledger, not in a fixture.

    A November exam reported as impossible in August is a false alarm, and false alarms
    are how a true one gets ignored. The fortnight stopped before the deadline; the work
    did not fail to fit.
    """
    add_commitment(conn, sett, "Exam 3 via LockDown Browser", minutes=4_000,
                   due=MONDAY + timedelta(days=70), estimate_source="analyzed")

    plan = runway.allocate(conn, sett, pool(conn, sett), MONDAY)

    assert plan.unreachable == []


def test_undated_work_is_never_called_impossible(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """It cannot miss a date it does not have. It is `PRIORITY_REST`, and it waits."""
    add_commitment(conn, sett, "read the whole library", minutes=9_000, due=None,
                   estimate_source="analyzed")

    plan = runway.allocate(conn, sett, pool(conn, sett), MONDAY)

    assert plan.unreachable == []


def test_overdue_work_is_allocated_as_early_as_there_is_room(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """The other live-ledger bug, and the worse of the two.

    An overdue obligation has no deadline left to beat. Cutting it off at a date in the
    past gave it *nothing* — every day of the horizon is after it — so the first draft
    allocated zero minutes to the five most urgent things on the owner's board and then
    reported them as impossible. The only useful answer for late work is "as early as
    there is room".
    """
    cid = add_commitment(conn, sett, "overdue recitation", minutes=30,
                         due=MONDAY - timedelta(days=6))

    plan = runway.allocate(conn, sett, pool(conn, sett), MONDAY)

    assert days_for(plan, cid) == [MONDAY]
    assert cid not in [c.commitment_id for c in plan.unreachable]


# ── the day planner reads it ──────────────────────────────────────────────


def test_allocated_work_outranks_merely_due_this_week(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """The band exists because "due this week" is a calendar fact that knows nothing
    about whether the week has room, and the runway has already weighed both."""
    cid = add_commitment(conn, sett, "starts today", minutes=60,
                         due=MONDAY + timedelta(days=20))

    items = planner.candidates(conn, sett, MONDAY, set(), allocated_today={cid})

    got = next(c for c in items if c.commitment_id == cid)
    assert got.priority == planner.PRIORITY_ALLOCATED_TODAY
    assert planner.PRIORITY_ALLOCATED_TODAY < planner.PRIORITY_DUE_THIS_WEEK


def test_the_allocation_never_outranks_a_promise_coming_due(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """A forecast must not beat a fact. Due today and at-risk are both statements the
    runway does not make."""
    assert planner.PRIORITY_DUE_TODAY < planner.PRIORITY_ALLOCATED_TODAY
    assert planner.PRIORITY_AT_RISK_GOAL < planner.PRIORITY_ALLOCATED_TODAY
    assert planner.PRIORITY_OVERDUE < planner.PRIORITY_ALLOCATED_TODAY


def test_the_plan_never_warns_about_work_it_just_scheduled(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """Seen on the first live run: a warning that a quiz would not finish in time,
    printed nine lines under the 7:00am block scheduling it.

    The runway walks the fortnight in deadline order against a day's total free minutes;
    `select` fills today in priority order with a homework reservation and a protected
    block charged up front. They are not the same algorithm and they will disagree at the
    margin. A plan that contradicts itself on one screen is worse than one that says
    less — rule 1 — so `select` wins, always.
    """
    # Single-letter names, because "assignment 1" is a substring of "assignment 11" and
    # a test that matches on that reports a contradiction the plan did not make.
    for i, letter in enumerate("ABCDEFGHIJKLMN"):
        add_commitment(conn, sett, f"assignment {letter}", minutes=90,
                       due=MONDAY + timedelta(days=1), n=i, estimate_source="analyzed")

    proposal = planner.propose(conn, sett, MONDAY)

    planned = {
        int(b["commitment_id"])
        for b in proposal.blocks
        if b["kind"] in ("work", "protected", "small") and b["commitment_id"]
    }
    assert planned, "the fixture planned nothing, so the assertion below proves nothing"
    warning = next(
        (n for n in proposal.notes if "do not finish before they are due" in n), ""
    )
    named = {
        c.what
        for c in planner.candidates(conn, sett, MONDAY, set())
        if c.commitment_id in planned
    }
    for what in named:
        assert what not in warning, f"{what!r} is both planned and declared impossible"


def test_two_runs_over_an_unchanged_world_allocate_identically(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """Rule 3's spirit where it applies to a pure function: the walk is deterministic, or
    the day's priority band flips on a coin toss between two syncs."""
    for i in range(6):
        add_commitment(conn, sett, f"assignment {i}", minutes=75,
                       due=MONDAY + timedelta(days=i % 3 + 1), n=i)

    first = runway.allocate(conn, sett, pool(conn, sett), MONDAY)
    second = runway.allocate(conn, sett, pool(conn, sett), MONDAY)

    assert first.sittings == second.sittings
    assert [c.commitment_id for c in first.unreachable] == [
        c.commitment_id for c in second.unreachable
    ]


def test_a_day_under_the_capacity_floor_is_promised_nothing(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """P3: the planner declines to plan such a day. Promising an assignment a day the
    planner is going to refuse is a forecast of something that cannot happen."""
    saturday = date(2026, 9, 5)
    cid = add_commitment(conn, sett, "weekend work", minutes=60,
                         due=saturday + timedelta(days=3))

    plan = runway.allocate(conn, sett, pool(conn, sett, saturday), saturday)

    assert saturday not in days_for(plan, cid)


def test_the_runway_prints_a_day_for_every_allocated_obligation(conn, sett, capsys) -> None:  # type: ignore[no-untyped-def]
    """`backglass plan --runway`, which is where the owner actually reads this.

    Behind a flag: the plan is a statement about today, and fourteen days of table under
    it every morning would be the longest thing on the screen. The daily sentence is the
    one about what will not finish.
    """
    from backglass.__main__ import _echo_runway

    for i, letter in enumerate("ABC"):
        add_commitment(conn, sett, f"assignment {letter}", minutes=60,
                       due=MONDAY + timedelta(days=i + 1), n=i)

    _echo_runway(conn, sett, MONDAY)

    printed = capsys.readouterr().out
    for letter in "ABC":
        assert f"assignment {letter}" in printed
    assert MONDAY.strftime("%a %d") in printed


def test_the_runway_names_in_full_what_the_note_had_to_elide(conn, sett, capsys) -> None:  # type: ignore[no-untyped-def]
    """The note caps its list at three. Seeing the rest is most of the reason to open
    the runway at all, so it repeats them rather than eliding them a second time."""
    for i, letter in enumerate("ABCDE"):
        add_commitment(conn, sett, f"impossible {letter}", minutes=4_000,
                       due=MONDAY + timedelta(days=1), n=i, estimate_source="analyzed")

    _echo = __import__("backglass.__main__", fromlist=["_echo_runway"])._echo_runway
    _echo(conn, sett, MONDAY)

    printed = capsys.readouterr()
    for letter in "ABCDE":
        assert f"impossible {letter}" in printed.err


# ── the fortnight is a window, and it says so ─────────────────────────────


def test_work_the_fortnight_never_reaches_is_stated_rather_than_dropped(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """Found on the live ledger, not in a fixture.

    267 obligations went into the allocation and 137 came out with a day. The other 130
    — 89 of them Canvas assignments — were due past the horizon and the horizon had no
    minutes left, so they earned no sitting, no `unreachable` line and no mention: the
    runway read as the whole board while it was the half of it that fitted. P2's rule is
    that overflow is stated and never silently truncated, and a forecast is not exempt.
    """
    cid = add_commitment(conn, sett, "November exam", minutes=4_000,
                         due=MONDAY + timedelta(days=70), estimate_source="analyzed")

    plan = runway.allocate(conn, sett, pool(conn, sett), MONDAY)

    assert [u.item.commitment_id for u in plan.beyond] == [cid]
    # Still not a failure: nothing is late, so nothing is called impossible.
    assert plan.unreachable == []


def test_only_the_remainder_of_partly_allocated_work_is_carried(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """An assignment can get four sittings and still not finish inside the fortnight.
    Counting its whole estimate as unreached would double-count the part that has a day,
    and the number under the panel is meant to be added to the list above it."""
    cid = add_commitment(conn, sett, "long reading", minutes=4_000,
                         due=MONDAY + timedelta(days=70), estimate_source="analyzed")

    plan = runway.allocate(conn, sett, pool(conn, sett), MONDAY)

    placed = sum(s.minutes for s in plan.sittings[cid])
    assert placed > 0
    assert plan.beyond[0].minutes == 4_000 - placed


def test_work_that_fits_is_not_reported_as_unreached(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """The sentence has to disappear with the fact, or it is noise on every open."""
    add_commitment(conn, sett, "one hour of it", minutes=60,
                   due=MONDAY + timedelta(days=3))

    plan = runway.allocate(conn, sett, pool(conn, sett), MONDAY)

    assert plan.beyond == []


def test_undated_work_is_never_reported_as_unreached(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """"Due after the fortnight" is a false statement about something with no date. It
    is `PRIORITY_REST`, the module already says so, and it waits."""
    add_commitment(conn, sett, "read the whole library", minutes=9_000, due=None,
                   estimate_source="analyzed")

    plan = runway.allocate(conn, sett, pool(conn, sett), MONDAY)

    assert plan.beyond == []


def test_the_runway_prints_what_the_fortnight_could_not_reach(conn, sett, capsys) -> None:  # type: ignore[no-untyped-def]
    """Counted, with three named. The same shape `_echo_overflow` settled on: a hundred
    restatements of "due in November" is a wall nobody reads, and printing nothing at all
    is how two thirds of the board became invisible."""
    from backglass.__main__ import _echo_runway

    for i in range(4):
        add_commitment(conn, sett, f"far assignment {i}", minutes=4_000, n=i,
                       due=MONDAY + timedelta(days=60 + i), estimate_source="analyzed")

    _echo_runway(conn, sett, MONDAY)

    printed = capsys.readouterr().out
    assert "obligation(s) has no day in the next 14" in printed
    assert "far assignment 0" in printed
