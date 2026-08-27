"""docs/04 §5 acceptance criteria, plus the requirements they rest on.

The nine bullets in §5 are the exit criterion for Phase 4 (docs/09), so each one has a
test named after it. Everything else here exists because a §5 bullet cannot be trusted
without it — a capacity model that happens to produce the right total by cancelling two
errors would pass the first bullet and fail in week two.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from backglass.config import Settings
from backglass.db import now_iso
from backglass.goals import checklist, checkpoints, health
from backglass.goals import targets as targets_mod
from backglass.ledger import USER_ID, Ledger
from backglass.plan import capacity as capacity_mod
from backglass.plan import estimates, planner, rollover, timezones
from backglass.plan.capacity import FixedEvent

PHOENIX = "America/Phoenix"
KOLKATA = "Asia/Kolkata"

# 2026-07-30 is a Thursday. A midweek working day, so nothing here depends on a weekend
# rule accidentally doing the work.
THURSDAY = date(2026, 7, 30)
MONDAY = date(2026, 7, 27)
FRIDAY = date(2026, 7, 31)


@pytest.fixture
def sett(settings: Settings) -> Settings:
    return settings.model_copy(update={"default_tz": PHOENIX, "confidence_threshold": 0.7})


def at(day: date, hhmm: str, tz: str = PHOENIX) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(tz))


def meeting(day: date, start: str, end: str, title: str = "Meeting", travel: bool = False):  # type: ignore[no-untyped-def]
    return FixedEvent(
        starts_at=at(day, start), ends_at=at(day, end), title=title, travel=travel
    )


def add_commitment(
    conn,
    sett: Settings,
    what: str,
    *,
    minutes: int = 60,
    due: date | None = None,  # type: ignore[no-untyped-def]
    n: int = 1,
    goal_id: int | None = None,
    rollovers: int = 0,
    occurred: str | None = None,
    estimate_source: str = "manual",
) -> int:
    ledger = Ledger(conn, sett)
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at, "
        " author, title, body_text, raw_json, content_hash, triage_verdict) "
        "VALUES (?, 'gmail:personal', ?, ?, ?, 'Dana <dana@example.gov>', ?, 'b', "
        " '{}', ?, 'keep')",
        (USER_ID, f"c{n}", now_iso(), occurred or "2026-07-20T09:00:00-07:00", what, f"h{n}"),
    )
    source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, counterparty_entity_id, what, due_at, "
        " estimated_minutes, estimate_source, confidence, status, source_item_id, "
        " created_at, goal_id, rollover_count) "
        "VALUES (?, 'i_owe', ?, ?, ?, ?, ?, 0.9, 'open', ?, ?, ?, ?)",
        (
            USER_ID,
            ledger.resolve_entity("Dana <dana@example.gov>"),
            what,
            due.isoformat() if due else None,
            minutes,
            estimate_source,
            source_id,
            now_iso(),
            goal_id,
            rollovers,
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def a_goal(
    conn,
    *,
    title: str = "Ship v1",
    target_date: date | None = None,  # type: ignore[no-untyped-def]
    weekly: int = 2,
    minutes_each: int = 90,
) -> tuple[int, int]:
    conn.execute(
        "INSERT INTO goal (user_id, title, horizon, target_date, definition_of_done, "
        " created_at) VALUES (?, ?, 'quarterly', ?, 'in production', ?)",
        (USER_ID, title, target_date.isoformat() if target_date else None, now_iso()),
    )
    goal_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO target (goal_id, kind, title, weekly_count, estimated_minutes_each, "
        " created_at) VALUES (?, 'cadence', 'Ship something', ?, ?, ?)",
        (goal_id, weekly, minutes_each, now_iso()),
    )
    return goal_id, int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


# ══ §5 bullet 1 ═══════════════════════════════════════════════════════════
# "A day with 5 hours of meetings never receives a plan containing more than
#  capacity_minutes of work."


def test_five_hours_of_meetings_never_yields_more_than_capacity(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    events = [
        meeting(THURSDAY, "09:00", "11:00", "Board"),
        meeting(THURSDAY, "11:30", "13:00", "Client"),
        meeting(THURSDAY, "14:00", "15:30", "Vendor"),
    ]
    for index in range(8):
        add_commitment(conn, sett, f"deliverable {index}", minutes=60, n=index + 1)

    cap = capacity_mod.compute(conn, sett, THURSDAY, events=events)
    proposal = planner.propose(conn, sett, THURSDAY, events=events)

    assert cap.fixed_minutes == 300, "five hours of meetings"
    assert proposal.planned_minutes <= cap.capacity_minutes, (
        f"planned {proposal.planned_minutes} > capacity {cap.capacity_minutes}"
    )
    assert proposal.overflow, "P2: what did not fit is reported, not dropped silently"
    assert any("did not fit" in note for note in proposal.notes)


def test_capacity_subtracts_fixed_buffer_travel_and_reserve(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """docs/04 §1.2, the formula, term by term."""
    events = [
        meeting(THURSDAY, "10:00", "11:00", "Long"),  # >= 30m → 10m buffer
        meeting(THURSDAY, "11:30", "11:45", "Short"),  # < 30m  → 5m buffer
        meeting(THURSDAY, "13:00", "14:00", "Drive", travel=True),
    ]
    cap = capacity_mod.compute(conn, sett, THURSDAY, events=events)

    assert cap.window_minutes == 540  # 09:00–18:00
    assert cap.fixed_minutes == 75  # 60 + 15
    assert cap.travel_minutes == 60
    assert cap.buffer_minutes == 25  # 10 + 5 + 10
    assert cap.reserve_minutes == 45


def test_the_reserve_is_never_zero(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """docs/04 §1.2: "The reserve is not optional and defaults to non-zero. A plan that
    fills every minute is a plan that fails at 10:15 and stays failed."."""
    zeroed = sett.model_copy(update={"daily_reserve_minutes": 0})
    cap = capacity_mod.compute(conn, zeroed, THURSDAY, events=[])
    assert cap.reserve_minutes >= 1


def test_p5_no_block_ever_crosses_a_fixed_event(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    events = [meeting(THURSDAY, "11:00", "12:00", "Immovable")]
    for index in range(6):
        add_commitment(conn, sett, f"task {index}", minutes=45, n=index + 1)
    proposal = planner.propose(conn, sett, THURSDAY, events=events)

    busy_start, busy_end = at(THURSDAY, "11:00"), at(THURSDAY, "12:00")
    for block in proposal.blocks:
        if block["kind"] == "fixed":
            continue
        start = datetime.fromisoformat(str(block["starts_at"]))
        end = datetime.fromisoformat(str(block["ends_at"]))
        assert end <= busy_start or start >= busy_end, block


def test_p5_no_two_blocks_overlap(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    for index in range(6):
        add_commitment(conn, sett, f"task {index}", minutes=45, n=index + 1)
    proposal = planner.propose(
        conn, sett, THURSDAY, events=[meeting(THURSDAY, "13:00", "13:30")]
    )

    spans = sorted(
        (datetime.fromisoformat(str(b["starts_at"])), datetime.fromisoformat(str(b["ends_at"])))
        for b in proposal.blocks
    )
    for (_, first_end), (second_start, _) in zip(spans, spans[1:], strict=False):
        assert second_start >= first_end


def test_p4_small_items_are_batched(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """ "Blocks have a minimum size of 25 minutes. Anything smaller is batched into a
    single 'small items' block."."""
    for index in range(4):
        add_commitment(conn, sett, f"quick thing {index}", minutes=10, n=index + 1)
    proposal = planner.propose(conn, sett, THURSDAY, events=[])

    small = [b for b in proposal.blocks if b["kind"] == "small"]
    assert len(small) == 1, "one batch, not four blocks"
    assert "4" in str(small[0]["title"])
    assert int(small[0]["minutes"]) >= sett.min_block_minutes


# ══ §5 bullet 2 ═══════════════════════════════════════════════════════════
# "A fully-booked day produces the 'no deep work available' line rather than an empty plan."


def test_a_fully_booked_day_says_so_rather_than_planning_nothing(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    events = [meeting(THURSDAY, "09:00", "18:00", "All-day workshop")]
    add_commitment(conn, sett, "something due", minutes=60, due=THURSDAY)
    proposal = planner.propose(conn, sett, THURSDAY, events=events)

    assert not proposal.capacity.plannable
    assert any("Fully booked" in note for note in proposal.notes)
    # P3: "list only what is due" — the work is reported as unplaced, not silently gone.
    assert proposal.overflow
    assert not [b for b in proposal.blocks if b["kind"] in ("work", "protected", "small")]


def test_p9_a_fragmented_day_names_the_missing_deep_work_block(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """ "No deep work block available today, calendar is fragmented." docs/04: "That
    sentence is the product."."""
    events = [
        meeting(THURSDAY, "09:40", "10:20"),
        meeting(THURSDAY, "11:00", "11:40"),
        meeting(THURSDAY, "12:20", "13:00"),
        meeting(THURSDAY, "13:40", "14:20"),
        meeting(THURSDAY, "15:00", "15:40"),
        meeting(THURSDAY, "16:20", "17:00"),
    ]
    add_commitment(conn, sett, "deep thing", minutes=90)
    proposal = planner.propose(conn, sett, THURSDAY, events=events)

    assert not proposal.protected_placed
    assert any("calendar is fragmented" in note for note in proposal.notes)


def test_p6_p7_the_protected_block_lands_in_the_peak_window(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    add_commitment(conn, sett, "draft the migration plan", minutes=90)
    proposal = planner.propose(conn, sett, THURSDAY, events=[])

    protected = [b for b in proposal.blocks if b["kind"] == "protected"]
    assert len(protected) == 1
    start = datetime.fromisoformat(str(protected[0]["starts_at"]))
    peak_start, peak_end = timezones.parse_window(sett.peak_window)
    assert peak_start <= start.time() < peak_end
    assert int(protected[0]["minutes"]) >= sett.protected_block_minutes


def test_p8_small_items_never_take_the_protected_block(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    add_commitment(conn, sett, "tiny admin", minutes=10, n=1)
    add_commitment(conn, sett, "draft the plan", minutes=120, n=2)
    proposal = planner.propose(conn, sett, THURSDAY, events=[])

    protected = [b for b in proposal.blocks if b["kind"] == "protected"]
    assert protected
    assert "tiny admin" not in str(protected[0]["title"])


# ══ §5 bullets 3 and 4 ════════════════════════════════════════════════════
# "An item proposed and not completed appears at the top of tomorrow's plan."
# "An item rolling over a third time triggers the drop-or-do question exactly once."


def test_an_uncompleted_item_leads_tomorrows_plan(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    add_commitment(conn, sett, "yesterday's work", minutes=60, n=1)
    planner.persist(conn, sett, planner.propose(conn, sett, THURSDAY, events=[]))
    rollover.close_day(conn, sett, THURSDAY, done_block_ids=set())

    # Arrives overnight and is due today, so it is a band above the rollover, which has no
    # due date at all. Corrected 2026-08-24: P10 orders what §1.5 rule 2 leaves tied, and
    # is not a licence for a dateless item to lead the day because it once rolled.
    add_commitment(conn, sett, "brand new work", minutes=60, n=2, due=FRIDAY)

    tomorrow = planner.propose(conn, sett, FRIDAY, events=[])
    work = [b for b in tomorrow.blocks if b["kind"] in ("protected", "work")]
    assert work
    assert "brand new work" in str(work[0]["title"]), (
        "§1.5 rule 2: what is due today leads what merely rolled"
    )
    titles = [str(b["title"]) for b in work]
    assert any("yesterday's work" in t for t in titles), "and the rollover is still planned"


# ══ 2026-08-24: the day was empty and the ledger was full ═════════════════
# Capacity is a sum, placement needs contiguity, and homework needs a claim.


class TestSelectionFitsTheDayItHas:
    """The defect these encode was measured, not imagined: on the owner's live
    2026-08-26 plan, 175 of 183 candidates overflowed and the day shipped one block,
    because `select` spent 215 fragmented minutes as though they were one run."""

    def test_an_indivisible_item_bigger_than_every_hole_is_overflow_not_a_spent_budget(
        self, conn, sett: Settings  # type: ignore[no-untyped-def]
    ) -> None:
        """The expensive half of the bug. The old code compared the sitting against the
        *sum*, took it, spent the minutes, and only found out at placement time — by
        which point the budget was gone and the work that would have fit was already
        overflow."""
        zone = ZoneInfo(sett.default_tz)

        def at(h: int, m: int) -> datetime:
            return datetime.combine(THURSDAY, time(h, m), tzinfo=zone)

        events = [
            FixedEvent(title="class", starts_at=at(*a), ends_at=at(*b))
            for a, b in (((10, 0), (11, 15)), ((12, 0), (13, 15)), ((14, 0), (15, 15)))
        ]
        big = add_commitment(conn, sett, "one long indivisible thing", minutes=200, n=1)
        small = add_commitment(conn, sett, "a real forty minutes", minutes=40, n=2)

        proposal = planner.propose(conn, sett, THURSDAY, events=events)

        placed = {b["commitment_id"] for b in proposal.blocks}
        assert small in placed, "the work that fits is planned"
        assert big in {c.commitment_id for c in proposal.overflow}
        assert big not in placed

    def test_divisible_work_lands_in_pieces_that_each_fit(
        self, conn, sett: Settings  # type: ignore[no-untyped-def]
    ) -> None:
        """A 344-minute milestone is planned across sittings (`Candidate.sitting`), and a
        sitting is now placed in pieces that each fit a real gap. Before splitting existed
        this was clamped to the largest hole instead — correct, and it threw away every
        other gap in the day."""
        zone = ZoneInfo(sett.default_tz)

        def at(h: int, m: int) -> datetime:
            return datetime.combine(THURSDAY, time(h, m), tzinfo=zone)

        # Nothing longer than 45 minutes is free anywhere in the window.
        events = [
            FixedEvent(title="class", starts_at=at(*a), ends_at=at(*b))
            for a, b in (
                ((9, 45), (11, 0)), ((11, 45), (13, 0)),
                ((13, 45), (15, 0)), ((15, 45), (17, 0)), ((17, 30), (18, 0)),
            )
        ]
        cid = add_commitment(
            conn, sett, "T - Final Analysis", minutes=344, n=1,
            due=THURSDAY, estimate_source="analyzed",
        )

        proposal = planner.propose(conn, sett, THURSDAY, events=events)

        mine = [b for b in proposal.blocks if b["commitment_id"] == cid]
        assert mine, "it is planned rather than dropped for want of one long run"
        assert all(int(b["minutes"]) <= 45 for b in mine), "every piece fits a real gap"
        assert all("of 344m left" in str(b["title"]) for b in mine), "each says it is part"


class TestTheDayAddsUp:
    """Owner, 2026-08-24: "find a way to fit it into the day"."""

    def test_the_protected_block_is_charged_to_the_budget(
        self, conn, sett: Settings  # type: ignore[no-untyped-def]
    ) -> None:
        """P1, which was quietly false. The protected slot is a fixed 90 minutes whatever
        the work inside it needs, and nobody charged the difference: on the owner's live
        2026-08-24 a 45-minute obligation sat in it and the plan claimed 248 minutes
        against a capacity of 205. Everything sized from "capacity left" then started
        from a negative number, which is why the Coding block silently did not exist."""
        # Saturated on purpose: the overrun is (protected_block_minutes - the head item's
        # own minutes), and it only shows once the budget is actually spent. Thirty-minute
        # items against a 90-minute protected slot is the shape the owner's real day had.
        for index in range(20):
            add_commitment(
                conn, sett, f"errand {index}", minutes=30, n=index + 1, due=THURSDAY,
            )

        proposal = planner.propose(conn, sett, THURSDAY, events=[])

        protected = [b for b in proposal.blocks if b["kind"] == "protected"]
        assert protected and int(protected[0]["minutes"]) > 30, "precondition: it overruns"
        assert proposal.planned_minutes <= proposal.capacity.capacity_minutes

    def test_a_small_task_takes_a_small_gap_and_leaves_the_long_run_alone(
        self, conn, sett: Settings  # type: ignore[no-untyped-def]
    ) -> None:
        """Best-fit. First-fit spends whatever run comes first on whatever task is
        ordered first, and a fragmented day has exactly one hole big enough for the
        60-minute thing."""
        zone = ZoneInfo(sett.default_tz)

        def at_(h: int, m: int) -> datetime:
            return datetime.combine(THURSDAY, time(h, m), tzinfo=zone)

        # Measured, not assumed: this leaves 09:00-10:00 (60m) and 11:30-12:00 (30m),
        # in that order — the long run FIRST, which is what makes first-fit and best-fit
        # disagree. Capacity is 89, enough for both items, so only placement decides.
        events = [
            FixedEvent(title="class", starts_at=at_(*a), ends_at=at_(*b))
            for a, b in (((10, 0), (11, 20)), ((12, 0), (18, 0)))
        ]
        # The 25-minute task is older, so it is ordered first and first-fit hands it the
        # hour — after which the hour of work has nowhere to go.
        add_commitment(conn, sett, "quick admin", minutes=25, n=1, due=THURSDAY,
                       occurred="2026-06-01T09:00:00-07:00")
        long_one = add_commitment(conn, sett, "the sixty minute thing", minutes=60, n=2,
                                  due=THURSDAY)

        # No protected block and no standing blocks: each would claim a slot before
        # placement runs, making this a test about those rules instead of this one.
        proposal = planner.propose(
            conn,
            sett.model_copy(update={
                "protected_block_minutes": 0, "daily_reserve_minutes": 1,
                "coding_block_minutes": 0, "study_block_minutes": 0,
            }),
            THURSDAY, events=events,
        )

        placed = {b["commitment_id"] for b in proposal.blocks}
        assert long_one in placed, "the only hour-long hole was left for the hour of work"

    def test_a_standing_block_splits_across_gaps_rather_than_shrinking(
        self, conn, sett: Settings  # type: ignore[no-untyped-def]
    ) -> None:
        """The owner's ask, literally: ninety minutes of coding on a day whose longest
        hole is forty-five should be two sittings, not forty-five minutes and a shrug."""
        zone = ZoneInfo(sett.default_tz)

        def at_(h: int, m: int) -> datetime:
            return datetime.combine(THURSDAY, time(h, m), tzinfo=zone)

        events = [
            FixedEvent(title="class", starts_at=at_(*a), ends_at=at_(*b))
            for a, b in (
                ((9, 45), (11, 0)), ((11, 45), (13, 0)),
                ((13, 45), (15, 0)), ((15, 45), (17, 0)), ((17, 30), (18, 0)),
            )
        ]

        proposal = planner.propose(
            conn,
            sett.model_copy(update={
                "protected_block_minutes": 0, "daily_reserve_minutes": 1,
                "study_block_minutes": 0,
            }),
            THURSDAY, events=events,
        )

        coding = [b for b in proposal.blocks if b["kind"] == "coding"]
        assert len(coding) == 2, "two sittings, not one shrunken one"
        assert sum(int(b["minutes"]) for b in coding) > 45
        assert all(int(b["minutes"]) >= sett.min_block_minutes for b in coding)
        assert {str(b["title"]) for b in coding} == {"Coding (1 of 2)", "Coding (2 of 2)"}

    def test_divisible_work_may_take_the_day_in_pieces_but_a_single_thing_may_not(
        self, conn, sett: Settings  # type: ignore[no-untyped-def]
    ) -> None:
        """The discriminator is evidence, not size — the same one `Candidate.sitting`
        uses. A milestone measured in pages is done across sittings by construction;
        "Move-in: Willow Hall 502" is three hours of one thing and cutting it into
        fragments would be a plan that cannot happen."""
        zone = ZoneInfo(sett.default_tz)

        def at_(h: int, m: int) -> datetime:
            return datetime.combine(THURSDAY, time(h, m), tzinfo=zone)

        events = [
            FixedEvent(title="class", starts_at=at_(*a), ends_at=at_(*b))
            for a, b in (((9, 40), (11, 0)), ((11, 40), (13, 0)), ((13, 40), (18, 0)))
        ]
        move = add_commitment(conn, sett, "Move-in: Willow Hall 502", minutes=90, n=1,
                              due=THURSDAY)

        proposal = planner.propose(
            conn, sett.model_copy(update={"protected_block_minutes": 0}),
            THURSDAY, events=events,
        )

        assert move in {c.commitment_id for c in proposal.overflow}
        assert move not in {b["commitment_id"] for b in proposal.blocks}

    def test_a_divisible_assignment_takes_the_day_in_pieces(
        self, conn, sett: Settings  # type: ignore[no-untyped-def]
    ) -> None:
        """The positive half of the same rule, and the one the owner asked for: an
        assignment measured off its own text may be spread across the gaps a class day
        leaves rather than waiting for a run the day does not have."""
        zone = ZoneInfo(sett.default_tz)

        def at_(h: int, m: int) -> datetime:
            return datetime.combine(THURSDAY, time(h, m), tzinfo=zone)

        events = [
            FixedEvent(title="class", starts_at=at_(*a), ends_at=at_(*b))
            for a, b in (((10, 0), (11, 20)), ((12, 0), (18, 0)))
        ]
        cid = add_commitment(
            conn, sett, "T - Final Analysis", minutes=85, n=1, due=THURSDAY,
            estimate_source="analyzed",
        )

        proposal = planner.propose(
            conn,
            sett.model_copy(update={
                "protected_block_minutes": 0, "daily_reserve_minutes": 1,
                "coding_block_minutes": 0, "study_block_minutes": 0,
            }),
            THURSDAY, events=events,
        )

        mine = [b for b in proposal.blocks if b["commitment_id"] == cid]
        assert len(mine) == 2, "60 minutes in one gap and 25 in the other"
        assert sum(int(b["minutes"]) for b in mine) == 85
        assert all("of 2)" in str(b["title"]) for b in mine), "each says it is a part"


class TestHomeworkGetsFirstClaim:
    """Owner's ruling, 2026-08-24: "more home work time". A reservation, not a target."""

    # A deliberately scarce day: 09:00-12:00, no standing blocks competing for the
    # remainder. Scarcity is the whole point — with capacity to spare a reservation
    # changes nothing and every test of it passes for the wrong reason.
    SCARCE = {"working_window": "09:00-12:00", "study_block_minutes": 0,
              "coding_block_minutes": 0, "protected_block_minutes": 0}

    def test_coursework_is_planned_ahead_of_other_work_that_would_have_eaten_the_day(
        self, conn, sett: Settings  # type: ignore[no-untyped-def]
    ) -> None:
        """Without the reservation the older errands take the scarce day in age order and
        the assignments overflow — which is what the owner's live board was doing."""
        scarce = sett.model_copy(update=self.SCARCE)
        # Older, so `order`'s age tiebreak puts them first inside the same band.
        for index in range(3):
            add_commitment(
                conn, scarce, f"errand {index}", minutes=60, n=index + 1, due=THURSDAY,
                occurred="2026-06-01T09:00:00-07:00",
            )
        homework = [
            add_commitment(
                conn, scarce, f"PSY101 assignment {index}", minutes=45, n=10 + index,
                due=THURSDAY, estimate_source="analyzed",
                occurred="2026-07-20T09:00:00-07:00",
            )
            for index in range(2)
        ]

        proposal = planner.propose(conn, scarce, THURSDAY, events=[])

        placed = {b["commitment_id"] for b in proposal.blocks}
        assert set(homework) <= placed, "coursework has first claim on the day"

    def test_the_reservation_lapses_when_there_is_no_coursework_to_want_it(
        self, conn, sett: Settings  # type: ignore[no-untyped-def]
    ) -> None:
        """Otherwise a day with nothing due is held artificially empty — two hours fenced
        off against homework that does not exist, and the errands overflow beside it."""
        scarce = sett.model_copy(update=self.SCARCE)
        # Two hours of errands against a three-hour day. Fenced off, the second one has
        # nowhere to go — which is the failure this asserts against.
        ids = [
            add_commitment(
                conn, scarce, f"errand {index}", minutes=60, n=index + 1, due=THURSDAY,
            )
            for index in range(2)
        ]

        proposal = planner.propose(conn, scarce, THURSDAY, events=[])

        placed = {b["commitment_id"] for b in proposal.blocks}
        assert set(ids) <= placed, "the whole scarce day is spent, none of it fenced off"

    def test_it_never_reserves_more_than_the_coursework_actually_wants(
        self, conn, sett: Settings  # type: ignore[no-untyped-def]
    ) -> None:
        """One twenty-five-minute quiz must not fence off two hours. The reservation caps
        at demand, so the errands behind it still get the rest of the scarce day."""
        scarce = sett.model_copy(update=self.SCARCE)
        quiz = add_commitment(
            conn, scarce, "a short quiz", minutes=25, n=1, due=THURSDAY,
            estimate_source="analyzed",
        )
        # The day holds 135 minutes. The quiz wants 25 of them, so a reservation that
        # ignored demand would fence off 120 and leave this errand 15.
        errand = add_commitment(
            conn, scarce, "errand", minutes=60, n=2, due=THURSDAY,
            occurred="2026-06-01T09:00:00-07:00",
        )

        proposal = planner.propose(conn, scarce, THURSDAY, events=[])

        placed = {b["commitment_id"] for b in proposal.blocks}
        assert quiz in placed
        assert errand in placed, "the other 110 minutes are still the day's to spend"


def test_a_rollover_leads_the_work_it_is_tied_with(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """P10, in the band where it applies — the half of the rule that survived the
    2026-08-24 correction, and the half a reader would otherwise assume was deleted."""
    add_commitment(conn, sett, "yesterday's work", minutes=60, n=1, due=FRIDAY)
    planner.persist(conn, sett, planner.propose(conn, sett, THURSDAY, events=[]))
    rollover.close_day(conn, sett, THURSDAY, done_block_ids=set())

    # Same band as the rollover: both due Friday, neither blocked on anyone.
    add_commitment(conn, sett, "brand new work", minutes=60, n=2, due=FRIDAY)

    tomorrow = planner.propose(conn, sett, FRIDAY, events=[])
    work = [b for b in tomorrow.blocks if b["kind"] in ("protected", "work")]
    assert work
    assert "yesterday's work" in str(work[0]["title"]), (
        "P10: among equals, what rolled goes first"
    )


def test_the_drop_or_do_question_fires_exactly_once(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """docs/04 §5, and P11. Asked once, then never again for that item."""
    from backglass.brief import daily

    add_commitment(conn, sett, "the thing that never happens", minutes=30, rollovers=3)

    first = daily.build(conn, sett, THURSDAY)
    asked = [line for line in first.all_lines() if "should it be dropped" in line.text]
    assert len(asked) == 1

    second = daily.build(conn, sett, THURSDAY)
    assert not [line for line in second.all_lines() if "should it be dropped" in line.text]


def test_rolling_over_increments_the_count_on_both_rows(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """P12. Denormalised onto the block on purpose (docs/04 §3) so a card render never
    walks history."""
    cid = add_commitment(conn, sett, "slippery task", minutes=60)
    planner.persist(conn, sett, planner.propose(conn, sett, THURSDAY, events=[]))
    report = rollover.close_day(conn, sett, THURSDAY, done_block_ids=set())

    assert report.rolled == 1
    assert (
        conn.execute("SELECT rollover_count FROM commitment WHERE id = ?", (cid,)).fetchone()[
            "rollover_count"
        ]
        == 1
    )
    assert (
        conn.execute(
            "SELECT rollover_count, outcome FROM plan_block WHERE commitment_id = ?", (cid,)
        ).fetchone()["rollover_count"]
        == 1
    )


def test_a_skipped_shutdown_infers_completion_and_never_nags(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """docs/04 §1.8: "If skipped, the planner infers completion from ledger state and marks
    the rest rollover. Never nag about a missed shutdown."."""
    done_id = add_commitment(conn, sett, "already finished", minutes=30, n=1)
    add_commitment(conn, sett, "not finished", minutes=30, n=2)
    planner.persist(conn, sett, planner.propose(conn, sett, THURSDAY, events=[]))
    conn.execute("UPDATE commitment SET status = 'done' WHERE id = ?", (done_id,))

    report = rollover.close_day(conn, sett, THURSDAY)  # no done_block_ids: skipped
    assert report.done == 1
    assert report.rolled == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM shutdown_note").fetchone()["n"] == 0


# ══ §5 bullet 5 ═══════════════════════════════════════════════════════════
# "Flying Phoenix to Coimbatore: the next brief leads with the timezone change and the
#  working window shifts. No block is scheduled at 03:00 local."


def test_flying_phoenix_to_coimbatore(conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    travelling = settings.model_copy(
        update={
            "default_tz": PHOENIX,
            # P14: an explicit range, never geolocation.
            "tz_ranges": [f"{FRIDAY.isoformat()}..2026-08-20:{KOLKATA}"],
        }
    )
    assert timezones.active_tz(travelling, THURSDAY) == PHOENIX
    assert timezones.active_tz(travelling, FRIDAY) == KOLKATA

    # P15: the brief leads with the change.
    assert timezones.changed_on(travelling, FRIDAY) == (PHOENIX, KOLKATA)
    assert timezones.changed_on(travelling, THURSDAY) is None

    # P13: the working window follows the active zone.
    start, end = timezones.window_on(travelling, FRIDAY)
    assert str(start.tzinfo) == KOLKATA
    assert start.strftime("%H:%M") == "09:00"

    add_commitment(conn, travelling, "work in Coimbatore", minutes=60)
    proposal = planner.propose(conn, travelling, FRIDAY, events=[])
    assert proposal.tz == KOLKATA
    assert any("Timezone changed" in note for note in proposal.notes)

    # "No block is scheduled at 03:00 local."
    for block in proposal.blocks:
        local = datetime.fromisoformat(str(block["starts_at"])).astimezone(ZoneInfo(KOLKATA))
        assert 9 <= local.hour < 18, f"{block['title']} at {local}"


def test_p16_a_call_shows_both_zones(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """ "A 09:00 Phoenix call is 21:30 in Coimbatore, and getting this wrong once costs a
    meeting."."""
    rendered = timezones.both_zones(at(THURSDAY, "09:00"), PHOENIX, KOLKATA)
    assert "09:00" in rendered
    assert "21:30" in rendered


# ══ §5 bullet 6 ═══════════════════════════════════════════════════════════
# "Monday planning reports committed hours against available hours, and names the gap
#  when targets exceed capacity."


def test_monday_names_the_capacity_gap(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    from backglass.brief import weekly

    # A five-day week yields 5 x (540 - 45) = 2475 minutes, about 41 hours. Thirty
    # sessions a week at 90 minutes is 45 hours, so this is genuinely over.
    a_goal(conn, weekly=30, minutes_each=90)
    brief = weekly.monday(conn, sett, MONDAY)

    assert brief.kind == "monday"
    text = " ".join(line.text for line in brief.all_lines())
    assert "Something has to give" in text
    assert "hours" in text


def test_g7_slack_is_reported_too(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """ "When under capacity by more than 25%, say that too. Slack is information."."""
    a_goal(conn, weekly=1, minutes_each=30)
    check = targets_mod.capacity_check(conn, sett, MONDAY)
    assert not check.over
    assert check.slack(sett)
    assert "spare" in (check.sentence(sett) or "")


def test_w1_monday_replaces_the_brief_rather_than_adding_to_it(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """ "Two emails on Monday means neither is read."."""
    from backglass.brief import daily

    add_commitment(conn, sett, "a normal commitment", due=MONDAY)
    brief = daily.build_for(conn, sett, MONDAY)
    assert brief.kind == "monday"
    assert not any(s.title in ("Slipping", "Awaiting others") for s in brief.sections)


def test_w2_the_friday_retro_appends_and_stays_under_150_words(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    from backglass.brief import daily, weekly

    for index in range(20):
        cid = add_commitment(
            conn, sett, f"finished item number {index} with a wordy name", n=index + 1
        )
        conn.execute(
            "UPDATE commitment SET status = 'done', resolved_at = ? WHERE id = ?",
            (now_iso(), cid),
        )
    section = weekly.friday(conn, sett, FRIDAY)
    words = len(" ".join(line.text for line in section.lines).split())
    assert words <= weekly.FRIDAY_WORD_LIMIT

    brief = daily.build_for(conn, sett, FRIDAY)
    assert brief.kind == "friday"
    assert any(s.title == "This week" for s in brief.sections)


# ══ §5 bullet 7 ═══════════════════════════════════════════════════════════
# "A goal with no checkpoints for 14 days shows serious staleness; a goal ahead of pace
#  with no checkpoints for 8 days shows stale but not at risk."


def test_staleness_and_risk_are_independent(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """G11. "Never merge into one 'health' score."

    The second half of the bullet is the interesting one: eight days quiet is *stale*, and
    a goal that is comfortably ahead of pace is simultaneously *not at risk*. A merged
    score cannot say both.
    """
    goal_id, target_id = a_goal(conn, target_date=THURSDAY + timedelta(days=90), weekly=1)
    # Plenty of past progress, nothing in the last eight days.
    for back in range(9, 30):
        checkpoints.record(
            conn,
            target_id,
            source="manual",
            occurred_at=(THURSDAY - timedelta(days=back)).isoformat() + "T09:00:00+00:00",
        )

    stale = {s.goal_id: s for s in health.staleness(conn, sett, THURSDAY)}[goal_id]
    risky = {r.goal_id: r for r in health.risk(conn, sett, THURSDAY)}[goal_id]

    assert stale.level == "warn"
    assert stale.days_quiet == 9
    assert stale.chip() == "9 days quiet"  # G12: a day count, never a bare colour
    assert not risky.at_risk, "ahead of pace, and staleness must not drag it into risk"


def test_fourteen_days_quiet_is_serious(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    goal_id, target_id = a_goal(conn)
    checkpoints.record(
        conn,
        target_id,
        source="manual",
        occurred_at=(THURSDAY - timedelta(days=15)).isoformat() + "T09:00:00+00:00",
    )
    stale = {s.goal_id: s for s in health.staleness(conn, sett, THURSDAY)}[goal_id]
    assert stale.level == "serious"
    assert stale.days_quiet == 15


def test_a_goal_can_be_fresh_and_at_risk(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """The other half of §2.5: "lots of activity, not enough to finish in time"."""
    goal_id, target_id = a_goal(conn, target_date=THURSDAY + timedelta(days=14), weekly=10)
    checkpoints.record(conn, target_id, source="manual", occurred_at=now_iso())

    stale = {s.goal_id: s for s in health.staleness(conn, sett, THURSDAY)}[goal_id]
    risky = {r.goal_id: r for r in health.risk(conn, sett, THURSDAY)}[goal_id]
    assert stale.level == "fresh"
    assert risky.at_risk


def test_g13_risk_reads_as_a_date_against_a_date(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    goal_id, target_id = a_goal(conn, target_date=THURSDAY + timedelta(days=14), weekly=10)
    checkpoints.record(conn, target_id, source="manual", occurred_at=now_iso())
    risky = {r.goal_id: r for r in health.risk(conn, sett, THURSDAY)}[goal_id]
    sentence = risky.sentence() or ""
    assert "on pace for" in sentence
    assert "target is" in sentence


def test_g4_a_target_missed_three_weeks_is_a_question_not_a_verdict(
    conn, sett: Settings
) -> None:  # type: ignore[no-untyped-def]
    """ "Lowering a target is a legitimate outcome, and framing it as one is the difference
    between a system that gets used and one that generates guilt."."""
    from backglass.brief import weekly

    a_goal(conn, weekly=3, minutes_each=30)
    flagged = health.unrealistic_targets(conn, sett, MONDAY)
    assert flagged and flagged[0].missed_weeks >= sett.unrealistic_after_weeks

    brief = weekly.monday(conn, sett, MONDAY)
    text = " ".join(line.text for line in brief.all_lines())
    assert "Lower it?" in text
    # Word-boundary matched: "again" must not appear as a word, but "against" in the
    # capacity sentence is fine and is not guilt copy.
    for guilt in ("failed", "you should", "again", "disappointing", "still"):
        assert not re.search(rf"\b{re.escape(guilt)}\b", text.lower()), guilt


def test_g8_a_commitment_links_to_at_most_one_goal(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    goal_a, _ = a_goal(conn, title="A")
    goal_b, _ = a_goal(conn, title="B")
    cid = add_commitment(conn, sett, "shared work")

    checkpoints.link_commitment(conn, cid, goal_a)
    checkpoints.link_commitment(conn, cid, goal_b)
    row = conn.execute("SELECT goal_id FROM commitment WHERE id = ?", (cid,)).fetchone()
    assert row["goal_id"] == goal_b, "the column is scalar; the second link replaces the first"


def test_g9_a_checkpoint_always_says_what_produced_it(conn) -> None:  # type: ignore[no-untyped-def]
    _, target_id = a_goal(conn)
    with pytest.raises(checkpoints.CheckpointError, match="source_item_id"):
        checkpoints.record(conn, target_id, source="extraction")
    with pytest.raises(checkpoints.CheckpointError, match="commitment_id"):
        checkpoints.record(conn, target_id, source="commitment")
    with pytest.raises(checkpoints.CheckpointError, match="unknown checkpoint source"):
        checkpoints.record(conn, target_id, source="vibes")


def test_g10_deleting_a_checkpoint_recomputes_progress(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    _, target_id = a_goal(conn, weekly=3)
    made = checkpoints.record(conn, target_id, source="manual", occurred_at=now_iso())
    before = targets_mod.progress(conn, sett, date.today())[0].done_this_week
    checkpoints.delete(conn, made.checkpoint_id)
    after = targets_mod.progress(conn, sett, date.today())[0].done_this_week
    assert before == 1
    assert after == 0


def test_g1_a_goal_with_no_target_is_named_on_monday(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    from backglass.brief import weekly

    conn.execute(
        "INSERT INTO goal (user_id, title, horizon, definition_of_done, created_at) "
        "VALUES (?, 'Inert thing', 'annual', 'done when done', ?)",
        (USER_ID, now_iso()),
    )
    brief = weekly.monday(conn, sett, MONDAY)
    text = " ".join(line.text for line in brief.all_lines())
    assert "A goal without a target is inert" in text


# ══ §5 bullet 8 ═══════════════════════════════════════════════════════════
# "Breaking a checklist streak produces no copy beyond the count resetting."


def test_breaking_a_streak_is_silent(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """C4. "No commiserating copy, no flame icons, no 'you lost your 40-day streak.'"."""
    from backglass.brief import daily

    item_id = checklist.add(conn, "Move for 30 minutes")
    for back in range(2, 6):
        checklist.tick(conn, item_id, THURSDAY - timedelta(days=back))
    # The gap: yesterday was missed.
    assert checklist.streak(conn, sett, item_id, THURSDAY) == 0

    brief = daily.build(conn, sett, THURSDAY)
    text = " ".join(line.text for line in brief.all_lines()) + " ".join(
        note.text for note in brief.notes
    )
    for banned in ("streak", "lost", "broke", "missed", "🔥"):
        assert banned not in text.lower()


def test_c1_the_cap_is_enforced_not_advisory(conn) -> None:  # type: ignore[no-untyped-def]
    for index in range(checklist.CAP):
        checklist.add(conn, f"habit {index}")
    with pytest.raises(checklist.ChecklistError, match="invisible"):
        checklist.add(conn, "one too many")


def test_c3_and_c6_a_weekday_scoped_streak_survives_the_weekend(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """C6: "a timezone change does not retroactively break a streak". The mechanism is
    that only *scheduled* days count, so an unscheduled Saturday is not a gap."""
    weekdays = 0b0011111  # Mon–Fri
    item_id = checklist.add(conn, "Inbox to zero", weekday_mask=weekdays)
    # Thursday 30 Jul back through the previous Friday, skipping the weekend.
    for day in (date(2026, 7, 29), date(2026, 7, 28), date(2026, 7, 27), date(2026, 7, 24)):
        checklist.tick(conn, item_id, day)
    assert checklist.streak(conn, sett, item_id, THURSDAY) == 4


def test_c5_the_checklist_is_only_in_the_brief_when_incomplete(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    from backglass.brief import daily

    item_id = checklist.add(conn, "Read something long")
    assert any(s.title == "Checklist" for s in daily.build(conn, sett, THURSDAY).sections)

    checklist.tick(conn, item_id, THURSDAY)
    assert not any(s.title == "Checklist" for s in daily.build(conn, sett, THURSDAY).sections)


# ══ §5 bullet 9 ═══════════════════════════════════════════════════════════
# "Regenerating a day plan supersedes rather than deletes the prior one."


def test_regenerating_supersedes_and_keeps_the_prior_plan(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """docs/04 §3: "You will want the history the first time you ask whether the planner
    is actually any good."."""
    add_commitment(conn, sett, "work", minutes=60)
    first = planner.persist(conn, sett, planner.propose(conn, sett, THURSDAY, events=[]))
    second = planner.persist(conn, sett, planner.propose(conn, sett, THURSDAY, events=[]))

    rows = {
        int(r["id"]): str(r["status"]) for r in conn.execute("SELECT id, status FROM day_plan")
    }
    assert len(rows) == 2, "the prior plan is kept"
    assert rows[first] == "superseded"
    assert rows[second] == "proposed"


# ══ estimates ═════════════════════════════════════════════════════════════


def test_type_defaults_match_docs04(sett: Settings) -> None:
    """ "review 30, draft 60, decision 15, meeting-prep 30, message 15, call 20,
    form 30, errand 30, log 10, unknown 45"."""
    table = estimates.defaults(sett)
    assert table == {
        "review": 30,
        "draft": 60,
        "decision": 15,
        "meeting_prep": 30,
        "message": 15,
        "call": 20,
        "form": 30,
        "errand": 30,
        "log": 10,
        "unknown": 45,
    }


@pytest.mark.parametrize(
    ("what", "kind"),
    [
        ("review the contract", "review"),
        ("draft the migration plan", "draft"),
        ("decide on the vendor", "decision"),
        ("board prep", "meeting_prep"),
        ("the thing", "unknown"),
    ],
)
def test_estimates_are_classified_by_type(what: str, kind: str, sett: Settings) -> None:
    assert estimates.estimate_for(what, sett).kind == kind


def test_a_manual_estimate_is_sticky(sett: Settings) -> None:
    """ "The owner can override on any item, and an override is sticky for that item."

    A later change to the type-default table must not silently overwrite it.
    """
    estimate = estimates.estimate_for(
        "draft the plan", sett, existing_minutes=15, existing_source="manual"
    )
    assert estimate.minutes == 15
    assert estimate.source == "manual"


def test_backfill_only_fills_what_is_missing(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    add_commitment(conn, sett, "review the contract", minutes=60, n=1)
    conn.execute("UPDATE commitment SET estimated_minutes = NULL, estimate_source = NULL")
    add_commitment(conn, sett, "draft the plan", minutes=15, n=2)  # manual, must survive

    assert estimates.backfill(conn, sett) == 1
    rows = {
        str(r["what"]): (r["estimated_minutes"], r["estimate_source"])
        for r in conn.execute("SELECT what, estimated_minutes, estimate_source FROM commitment")
    }
    assert rows["review the contract"] == (30, "type_default")
    assert rows["draft the plan"] == (15, "manual")


def test_estimates_are_never_auto_adjusted(conn) -> None:  # type: ignore[no-untyped-def]
    """docs/04 §1.3: "Do not auto-adjust silently." `ratio_report` computes a number and
    writes nothing.

    The thin-sample half of this used to assert `sentence() is None`. Silence was the
    mechanism, not the rule — and on the owner's real ledger it meant the loop had been
    doing nothing for months with no surface saying why (2 finished blocks in 94 days).
    Since 2026-08-27 it describes its own emptiness instead. What must not change is that
    it draws no *conclusion* from a sample too small to have one, which is what is
    asserted here now.
    """
    import inspect

    source = inspect.getsource(estimates.ratio_report)
    assert "UPDATE" not in source.upper()
    report = estimates.ratio_report(conn)
    assert not report.ready
    said = report.sentence() or ""
    assert "type defaults" not in said, "it advised a change on no evidence"
    assert "within 5%" not in said


# ══ ordering ══════════════════════════════════════════════════════════════


def test_ordering_follows_docs04_precedence(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """docs/04 §1.5. Overdue before due-today before at-risk-goal before due-this-week."""
    goal_id, _ = a_goal(conn, target_date=THURSDAY + timedelta(days=7), weekly=10)
    add_commitment(conn, sett, "overdue thing", n=1, due=THURSDAY - timedelta(days=1))
    add_commitment(conn, sett, "due today thing", n=2, due=THURSDAY)
    add_commitment(conn, sett, "goal thing", n=3, goal_id=goal_id)
    add_commitment(conn, sett, "later thing", n=4, due=THURSDAY + timedelta(days=30))

    pool = planner.candidates(conn, sett, THURSDAY, {goal_id})
    ordered = [c.what for c in planner.order(pool)]
    assert ordered.index("overdue thing") < ordered.index("due today thing")
    assert ordered.index("due today thing") < ordered.index("goal thing")
    assert ordered.index("goal thing") < ordered.index("later thing")


def test_asks_of_other_people_go_early(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """docs/04 §1.5 rule 3: "Blocked-on-others items get scheduled early in the day, so
    the ask goes out with a full working day left for a response."."""
    add_commitment(conn, sett, "chase Dana for the SOW", n=1, due=THURSDAY + timedelta(days=5))
    add_commitment(conn, sett, "write the report", n=2, due=THURSDAY + timedelta(days=5))
    ordered = [c.what for c in planner.order(planner.candidates(conn, sett, THURSDAY, set()))]
    assert ordered[0] == "chase Dana for the SOW"


def test_owed_to_me_is_never_scheduled(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """Scheduling time for someone else's work would be scheduling time to wait."""
    add_commitment(conn, sett, "my work", n=1)
    conn.execute("UPDATE commitment SET direction = 'owed_to_me' WHERE what = 'my work'")
    assert planner.candidates(conn, sett, THURSDAY, set()) == []


def test_a_non_working_day_has_no_capacity(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    saturday = date(2026, 8, 1)
    cap = capacity_mod.compute(conn, sett, saturday, events=[])
    assert cap.capacity_minutes == 0
    assert cap.slots == []
    assert not cap.plannable


def test_g13_a_cross_year_projection_carries_its_year(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """A projection that slips into next year rendered as "5 Jun" reads as the past
    against a "28 Sep" target. The year appears exactly when the two dates differ on it."""
    goal_id, target_id = a_goal(conn, target_date=date(2026, 9, 28), weekly=10)
    # One checkpoint a month ago: observed rate ~0.25/wk against 10/wk needed, so the
    # projection lands years out.
    checkpoints.record(
        conn,
        target_id,
        source="manual",
        occurred_at=(THURSDAY - timedelta(days=20)).isoformat() + "T09:00:00+00:00",
    )
    risky = {r.goal_id: r for r in health.risk(conn, sett, THURSDAY)}[goal_id]
    sentence = risky.sentence() or ""
    assert risky.at_risk
    assert "target is 28 Sep 2026" in sentence
    assert re.search(r"on pace for \d+ \w+ 20\d\d", sentence), sentence


# ─────────────────────────────────────── the login catch-up run (--if-missing)


class TestCatchUpPlan:
    """`plan --if-missing`, the command `com.backglass.plan-catchup` runs at login.

    A LaunchAgent fires `RunAtLoad` on every login, and `StartCalendarInterval` cannot
    cover a machine that was powered off at 05:45. So the catch-up must plan a day that
    has no plan and must be a no-op on one that does — otherwise opening the laptop at
    10:00 would silently supersede a plan already half-worked.
    """

    def _plan(self, conn, sett: Settings):  # type: ignore[no-untyped-def]
        proposal = planner.propose(conn, sett, THURSDAY, at_risk_goals=set())
        return planner.persist(conn, sett, proposal)

    def _run(  # type: ignore[no-untyped-def]
        self, sett: Settings, monkeypatch: pytest.MonkeyPatch, *args: str
    ):
        from typer.testing import CliRunner

        import backglass.__main__ as cli

        monkeypatch.setattr(cli, "get_settings", lambda: sett)
        return CliRunner().invoke(cli.app, ["plan", "--date", THURSDAY.isoformat(), *args])

    def test_no_live_plan_before_anything_is_planned(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        assert planner.current_plan_id(conn, THURSDAY) is None

    def test_the_live_plan_is_the_newest_non_superseded_one(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        add_commitment(conn, sett, "a thing", n=1, due=THURSDAY)
        first = self._plan(conn, sett)
        second = self._plan(conn, sett)
        assert first != second
        assert planner.current_plan_id(conn, THURSDAY) == second

    def test_a_plan_for_another_day_does_not_count(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        self._plan(conn, sett)
        assert planner.current_plan_id(conn, FRIDAY) is None

    def test_it_plans_a_day_that_has_none(  # type: ignore[no-untyped-def]
        self, conn, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._run(sett, monkeypatch, "--if-missing")
        assert result.exit_code == 0, result.output
        assert planner.current_plan_id(conn, THURSDAY) is not None

    def test_a_second_run_writes_nothing(  # type: ignore[no-untyped-def]
        self, conn, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Idempotency (CLAUDE.md rule 3) is what makes RunAtLoad safe at every login.
        self._run(sett, monkeypatch, "--if-missing")
        before = planner.current_plan_id(conn, THURSDAY)
        rows = conn.execute("SELECT COUNT(*) AS n FROM day_plan").fetchone()["n"]

        result = self._run(sett, monkeypatch, "--if-missing")

        assert result.exit_code == 0, result.output
        assert "already planned" in result.output
        assert planner.current_plan_id(conn, THURSDAY) == before
        assert conn.execute("SELECT COUNT(*) AS n FROM day_plan").fetchone()["n"] == rows

    def test_it_never_replaces_an_accepted_plan(  # type: ignore[no-untyped-def]
        self, conn, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._run(sett, monkeypatch, "--accept")
        accepted = planner.current_plan_id(conn, THURSDAY)

        self._run(sett, monkeypatch, "--if-missing")

        row = conn.execute("SELECT status FROM day_plan WHERE id = ?", (accepted,)).fetchone()
        assert row["status"] == "accepted"
        assert planner.current_plan_id(conn, THURSDAY) == accepted

    def test_without_the_flag_a_rerun_still_regenerates(  # type: ignore[no-untyped-def]
        self, conn, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The 05:45 job carries no --if-missing: an explicit `backglass plan` must keep
        # superseding, or the owner could never ask for a fresh plan after a re-sync.
        self._run(sett, monkeypatch)
        first = planner.current_plan_id(conn, THURSDAY)
        self._run(sett, monkeypatch)
        assert planner.current_plan_id(conn, THURSDAY) != first


# ── confirmed plans occupy the day the way meetings do ──────────────────────


def add_engagement(  # type: ignore[no-untyped-def]
    conn,
    *,
    what: str,
    starts_at: str | None,
    ends_at: str | None = None,
    status: str = "confirmed",
    location: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO engagement (user_id, kind, what, starts_at, ends_at, "
        " when_is_explicit, location, status, confidence, source_item_id, created_at) "
        "VALUES (?, 'social', ?, ?, ?, 1, ?, ?, 0.9, ?, ?)",
        (
            USER_ID,
            what,
            starts_at,
            ends_at,
            location,
            status,
            _any_source_item(conn),
            now_iso(),
        ),
    )
    return int(cursor.lastrowid or 0)


def _any_source_item(conn) -> int:  # type: ignore[no-untyped-def]
    """engagement.source_item_id is NOT NULL — every plan cites the message it came
    from, so a fixture has to supply one rather than pass NULL."""
    row = conn.execute("SELECT id FROM source_item LIMIT 1").fetchone()
    if row is not None:
        return int(row["id"])
    cursor = conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " content_hash) VALUES (?, 'manual', 'plan-fixture', ?, ?, 'h')",
        (USER_ID, now_iso(), now_iso()),
    )
    return int(cursor.lastrowid or 0)


class TestEngagementsOnTheDay:
    def test_a_confirmed_plan_subtracts_from_capacity(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        """Dinner at seven is not time available for deep work."""
        before = capacity_mod.compute(conn, sett, THURSDAY).capacity_minutes
        add_engagement(
            conn, what="dinner at Ravi's", starts_at=f"{THURSDAY.isoformat()}T11:00:00-07:00"
        )
        after = capacity_mod.compute(conn, sett, THURSDAY).capacity_minutes
        assert after < before

    def test_a_proposed_plan_does_not_touch_the_day(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        """Someone suggesting Thursday is not Thursday.

        Reserving time for an unanswered invitation would let anyone who emails the owner
        delete an evening from their week. Proposals are surfaced in the brief instead.
        """
        before = capacity_mod.compute(conn, sett, THURSDAY).capacity_minutes
        add_engagement(
            conn,
            what="drinks maybe",
            starts_at=f"{THURSDAY.isoformat()}T11:00:00-07:00",
            status="proposed",
        )
        after = capacity_mod.compute(conn, sett, THURSDAY).capacity_minutes
        assert after == before

    def test_a_plan_with_a_day_but_no_hour_is_not_placed(  # type: ignore[no-untyped-def]
        self, conn, sett: Settings
    ) -> None:
        """"Lunch on Thursday" has no honest hour. Parsed as an ISO date it would land at
        midnight and either vanish outside the window or block the top of the morning for
        something nobody said was in the morning.

        It is still *returned*, as an all-day banner. The assertion here used to be
        `engagement_events(...) == []`, which pinned the mechanism rather than the
        property: what the rule protects is that no capacity is deleted on a guess, and
        that is the line below that still holds. Dropping the plan from the day as well
        was a side effect nobody chose, and it hid six days of a summer programme.
        """
        before = capacity_mod.compute(conn, sett, THURSDAY).capacity_minutes
        add_engagement(conn, what="lunch sometime", starts_at=THURSDAY.isoformat())
        after = capacity_mod.compute(conn, sett, THURSDAY).capacity_minutes

        assert after == before, "no honest hour means no capacity spent"
        (event,) = capacity_mod.engagement_events(conn, THURSDAY, PHOENIX)
        assert event.allday is True
        assert event.kind == "allday"
        # Never at an hour: midnight to midnight is the span of a day, not a time in it.
        assert (event.starts_at.hour, event.starts_at.minute) == (0, 0)
        assert event.ends_at - event.starts_at == timedelta(days=1)

    def test_a_plan_with_no_stated_end_runs_an_hour(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        add_engagement(
            conn, what="coffee", starts_at=f"{THURSDAY.isoformat()}T11:00:00-07:00"
        )
        (event,) = capacity_mod.engagement_events(conn, THURSDAY, PHOENIX)
        assert event.minutes == 60

    def test_a_stated_end_wins_over_the_default(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        add_engagement(
            conn,
            what="conference session",
            starts_at=f"{THURSDAY.isoformat()}T11:00:00-07:00",
            ends_at=f"{THURSDAY.isoformat()}T14:00:00-07:00",
        )
        (event,) = capacity_mod.engagement_events(conn, THURSDAY, PHOENIX)
        assert event.minutes == 180

    def test_no_work_block_is_scheduled_across_a_confirmed_plan(  # type: ignore[no-untyped-def]
        self, conn, sett: Settings
    ) -> None:
        """P5 applies to a plan exactly as it does to a meeting."""
        add_engagement(
            conn,
            what="lunch with Priya",
            starts_at=f"{THURSDAY.isoformat()}T12:00:00-07:00",
            ends_at=f"{THURSDAY.isoformat()}T13:00:00-07:00",
        )
        for index in range(6):
            add_commitment(conn, sett, f"task {index}", minutes=60, n=index + 1)

        proposal = planner.propose(conn, sett, THURSDAY)

        busy_start, busy_end = at(THURSDAY, "12:00"), at(THURSDAY, "13:00")
        for block in proposal.blocks:
            if block["kind"] in ("fixed", "routine"):
                continue
            start = datetime.fromisoformat(str(block["starts_at"]))
            end = datetime.fromisoformat(str(block["ends_at"]))
            assert end <= busy_start or start >= busy_end, block

    def test_a_plan_on_another_day_is_left_alone(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        add_engagement(
            conn, what="dinner", starts_at=f"{FRIDAY.isoformat()}T18:00:00-07:00"
        )
        assert capacity_mod.engagement_events(conn, THURSDAY, PHOENIX) == []
        assert len(capacity_mod.engagement_events(conn, FRIDAY, PHOENIX)) == 1

    def test_a_plan_the_system_does_not_believe_takes_no_time(  # type: ignore[no-untyped-def]
        self, conn, sett: Settings
    ) -> None:
        """CLAUDE.md rule 2, on the surface where breaking it costs the most.

        The brief filters low-confidence plans and the capacity model did not, so a
        0.3-confidence guess silently deleted 70 minutes from a real day: a named fixed
        block the owner never agreed to, and less work planned with no explanation. The
        brief rendering nothing while the planner acts on it is the tell.
        """
        before = capacity_mod.compute(conn, sett, THURSDAY).capacity_minutes
        conn.execute(
            "INSERT INTO engagement (user_id, kind, what, starts_at, ends_at,"
            " when_is_explicit, location, status, confidence, source_item_id, created_at)"
            " VALUES (?, 'social', 'dubious lunch', ?, NULL, 1, NULL, 'confirmed',"
            " 0.3, ?, ?)",
            (
                USER_ID,
                f"{THURSDAY.isoformat()}T11:00:00-07:00",
                _any_source_item(conn),
                now_iso(),
            ),
        )

        after = capacity_mod.compute(conn, sett, THURSDAY)

        assert after.capacity_minutes == before
        # Routines are always on the day; the dubious plan is what must not be.
        assert [e.title for e in after.fixed if e.kind != "routine"] == []

    def test_one_meeting_counts_once_however_many_sources_described_it(  # type: ignore[no-untyped-def]
        self, conn, sett: Settings
    ) -> None:
        """Found on the owner's real ledger, not imagined.

        200 hand-imported `calendar:asu` rows plus Calendar.app's `calendar:apple` rows
        describe the same classes, and they store the same instant differently —
        `2026-08-20T10:30:00-07:00` against `2026-08-20T17:30:00.000Z` — so nothing
        textual catches it. Every class was subtracted twice and the day's capacity fell
        from ten hours to ten minutes.
        """
        for source, starts, ends in (
            ("calendar:asu", "2026-07-30T10:30:00-07:00", "2026-07-30T11:45:00-07:00"),
            ("calendar:apple", "2026-07-30T17:30:00.000Z", "2026-07-30T18:45:00.000Z"),
        ):
            payload = {
                "starts_at": starts,
                "ends_at": ends,
                "status": "confirmed",
                "declined": False,
                "travel": False,
            }
            conn.execute(
                "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
                " occurred_at, title, raw_json, content_hash) "
                "VALUES (?, ?, ?, ?, ?, 'HON 171', ?, ?)",
                (USER_ID, source, f"e-{source}", now_iso(), starts,
                 __import__("json").dumps(payload), f"h-{source}"),
            )

        capacity = capacity_mod.compute(conn, sett, THURSDAY)

        events = [e for e in capacity.fixed if e.kind != "routine"]
        assert len(events) == 1, [e.title for e in capacity.fixed]
        # 75 for the class; lunch and the in-window slice of gym add their own.
        assert capacity.fixed_minutes == 75 + 45 + 30

    def test_a_utc_stored_event_is_placed_in_the_owners_own_hours(  # type: ignore[no-untyped-def]
        self, conn, sett: Settings
    ) -> None:
        """Comparisons were always right; rendering was not.

        Two aware datetimes compare by instant whatever their offsets, so a UTC-stored
        event landed in the correct slot — and then every surface printed it with
        strftime, so a 10:30 Phoenix lecture read as "17:30". The owner's hand-imported
        calendar stores `-07:00` and printed correctly, so the two sat in one plan seven
        hours apart and work appeared to be scheduled at 01:00.
        """
        payload = {
            "starts_at": "2026-07-30T17:30:00.000Z",  # 10:30 in Phoenix
            "ends_at": "2026-07-30T18:45:00.000Z",
            "status": "confirmed",
            "declined": False,
            "travel": False,
        }
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, title, raw_json, content_hash) "
            "VALUES (?, 'calendar:apple', 'utc-1', ?, ?, 'HON 171', ?, 'h-utc')",
            (USER_ID, now_iso(), "2026-07-30T17:30:00.000Z",
             __import__("json").dumps(payload)),
        )

        (event,) = capacity_mod.fixed_events(conn, THURSDAY, PHOENIX)

        assert event.starts_at.strftime("%H:%M") == "10:30"
        assert event.ends_at.strftime("%H:%M") == "11:45"


# ── the weekend is a day too ────────────────────────────────────────────────────
#
# 2026-08-09 is a Sunday, and it is the day the owner moved into Willow Hall 502. The
# plan that morning held breakfast, lunch, gym, shower, dinner — and nothing else, with
# a 1.0-confidence commitment due that very day sitting at position N of a 48-item
# overflow. Three separate mechanisms produced that, and the tests below pin each.
SUNDAY = date(2026, 8, 9)
SATURDAY = date(2026, 8, 8)


@pytest.fixture
def weekends(sett: Settings) -> Settings:
    """The owner's real configuration after this change: seven planned days, and a
    weekend priced as a weekend rather than as a twelve-hour workday."""
    return sett.model_copy(
        update={
            "working_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "working_window": "10:00-22:00",
            "weekend_window": "10:00-18:00",
        }
    )


def test_a_weekend_off_the_working_days_list_has_no_window(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """The regression itself. Sunday is absent from the default `working_days`, so the
    day had no window, capacity was zero, and `propose` placed nothing at all."""
    assert timezones.is_working_day(sett, SUNDAY) is False

    add_commitment(conn, sett, "Move-in: Willow Hall 502, 8:00am", minutes=180, due=SUNDAY)
    proposal = planner.propose(conn, sett, SUNDAY, events=[])

    assert proposal.capacity.capacity_minutes == 0
    assert not [b for b in proposal.blocks if b["kind"] in ("work", "protected", "small")]
    assert len(proposal.overflow) == 1


def test_the_weekend_window_makes_sunday_plannable(conn, weekends: Settings) -> None:  # type: ignore[no-untyped-def]
    """And the fix. The same day, the same commitment, with the weekend planned."""
    assert timezones.is_working_day(weekends, SUNDAY) is True

    add_commitment(conn, weekends, "Move-in: Willow Hall 502, 8:00am", minutes=180, due=SUNDAY)
    proposal = planner.propose(conn, weekends, SUNDAY, events=[])

    assert proposal.capacity.capacity_minutes > 0
    placed = [b for b in proposal.blocks if b["kind"] in ("work", "protected")]
    # The protected slot is a fixed 90 minutes and this is three hours of one thing, so
    # the block says which part of it the day is giving — goal 4 increment C. The plan
    # always did truncate here; what changed is that it now admits to it.
    assert [b["title"] for b in placed] == [
        "Move-in: Willow Hall 502, 8:00am (90m of 180m left)"
    ]
    assert proposal.overflow == []


def test_the_weekend_window_is_shorter_than_the_weekday_one(weekends: Settings) -> None:
    """P13 still holds — this narrows the window, it does not detach it from the zone."""
    assert timezones.window_for(weekends, SUNDAY) == "10:00-18:00"
    assert timezones.window_for(weekends, SATURDAY) == "10:00-18:00"
    assert timezones.window_for(weekends, THURSDAY) == "10:00-22:00"

    start, end = timezones.window_on(weekends, SUNDAY)
    assert (start.strftime("%H:%M"), end.strftime("%H:%M")) == ("10:00", "18:00")
    assert str(start.tzinfo) == PHOENIX

    weekday_start, weekday_end = timezones.window_on(weekends, THURSDAY)
    assert (weekday_end - weekday_start) > (end - start)


def test_a_blank_weekend_window_means_the_weekday_one(sett: Settings) -> None:
    """Blank is "same as the weekday window", never "no window" — a day with no window
    is expressed by leaving it out of `working_days`, and two ways to say one thing is
    how one of them ends up wrong."""
    seven = sett.model_copy(
        update={
            "working_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "working_window": "09:00-18:00",
            "weekend_window": "",
        }
    )
    assert timezones.window_for(seven, SUNDAY) == "09:00-18:00"
    assert timezones.window_for(seven, THURSDAY) == "09:00-18:00"


def test_a_malformed_window_fails_at_startup(sett: Settings) -> None:
    """Same contract as `routines`: bad configuration raises when Settings is built,
    not at 05:45 inside the planner."""
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="expected HH:MM-HH:MM"):
        Settings(owner_name="K", db_path=sett.db_path, weekend_window="10am-6pm")

    # And the weekday one goes through the same door, which it did not before.
    with pytest.raises(pydantic.ValidationError, match="expected HH:MM-HH:MM"):
        Settings(owner_name="K", db_path=sett.db_path, working_window="all day")


# ── zero capacity has two causes, and they are opposite facts ───────────────────


def test_an_off_day_is_not_described_as_fully_booked(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """"Fully booked — 0m of capacity" was what the owner was told about the Sunday they
    moved house. Nothing was booked. The sentence sends a reader hunting for meetings
    that are not there, when the fact to act on is that Sunday has no window."""
    add_commitment(conn, sett, "Move-in: Willow Hall 502, 8:00am", minutes=180, due=SUNDAY)
    proposal = planner.propose(conn, sett, SUNDAY, events=[])

    assert proposal.capacity.no_window is True
    assert any("Sunday is not a working day" in note for note in proposal.notes)
    assert not any("Fully booked" in note for note in proposal.notes)


def test_a_genuinely_booked_weekday_still_says_fully_booked(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """The other half of the distinction. A window that exists and got eaten is the
    case P3 was written for, and it must keep its own sentence."""
    proposal = planner.propose(
        conn, sett, THURSDAY, events=[meeting(THURSDAY, "09:00", "18:00", "All-day workshop")]
    )

    assert proposal.capacity.no_window is False
    assert any("Fully booked" in note for note in proposal.notes)
    assert not any("not a working day" in note for note in proposal.notes)


def test_what_is_due_leads_instead_of_sitting_at_position_thirty(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """P3 says "list only what is due". The implementation handed back the whole ordered
    pool, so a 1.0-confidence obligation due today arrived somewhere inside forty-eight
    rows with nothing marking it. `due_now` is that subset, and it is a view of
    `overflow` rather than a second list — everything in it is still in there."""
    add_commitment(conn, sett, "Move-in: Willow Hall 502", minutes=180, due=SUNDAY, n=1)
    add_commitment(conn, sett, "overdue thing", minutes=30, due=date(2026, 8, 1), n=2)
    for i in range(3, 12):
        add_commitment(conn, sett, f"someday item {i}", minutes=30, n=i)

    proposal = planner.propose(conn, sett, SUNDAY, events=[])

    assert len(proposal.overflow) == 11
    assert {c.what for c in proposal.due_now} == {
        "Move-in: Willow Hall 502",
        "overdue thing",
    }
    assert all(c in proposal.overflow for c in proposal.due_now)
    assert any("2 due or overdue" in note for note in proposal.notes)
    assert any("Move-in: Willow Hall 502" in note for note in proposal.notes)


def test_a_day_with_nothing_due_gets_no_due_line(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """The note is earned, not decorative — an off day with only someday work says so
    once and stops talking."""
    add_commitment(conn, sett, "someday item", minutes=30)
    proposal = planner.propose(conn, sett, SUNDAY, events=[])

    assert proposal.overflow
    assert proposal.due_now == []
    assert not any("due or overdue" in note for note in proposal.notes)


class TestTheOverflowIsReadableAndStillComplete:
    """P2 forbids silent truncation. Printing all of it obeyed that the way a thirty-page
    contract obeys disclosure: 2026-08-09 ended in 46 lines, mostly restatements of each
    other, and a list nobody reads hides a due item exactly as well as dropping it would.

    So the CLI prints in full what did not fit AND is already due or overdue — the cases
    where not fitting is news — and counts the rest by band. These pin the property that
    makes that safe: the two parts partition the overflow, so the numbers on screen add
    up to the planner's own total and nothing leaves without being counted.
    """

    def _proposal(self, conn: sqlite3.Connection, sett: Settings):  # type: ignore[no-untyped-def]
        add_commitment(conn, sett, "overdue form", minutes=30, due=date(2026, 8, 1), n=1)
        add_commitment(conn, sett, "Move-in: Willow Hall 502", minutes=180, due=SUNDAY, n=2)
        for i in range(3, 14):
            add_commitment(conn, sett, f"someday item {i}", minutes=30, n=i)
        return planner.propose(conn, sett, SUNDAY, events=[])

    def test_every_item_is_either_printed_or_counted(
        self, conn: sqlite3.Connection, sett: Settings, capsys
    ) -> None:  # type: ignore[no-untyped-def]
        from backglass.__main__ import _echo_overflow

        proposal = self._proposal(conn, sett)
        _echo_overflow(proposal)
        printed = capsys.readouterr().err

        lines = [ln for ln in printed.splitlines() if "did not fit:" in ln]
        counted = int(re.search(r"· (\d+) more did not fit", printed).group(1))
        assert len(lines) + counted == len(proposal.overflow)

    def test_what_is_already_due_is_named_and_never_folded_into_a_count(
        self, conn: sqlite3.Connection, sett: Settings, capsys
    ) -> None:  # type: ignore[no-untyped-def]
        from backglass.__main__ import _echo_overflow

        _echo_overflow(self._proposal(conn, sett))
        printed = capsys.readouterr().err

        assert "did not fit: overdue form" in printed
        assert "did not fit: Move-in: Willow Hall 502" in printed
        assert "someday item 5" not in printed  # counted, not named

    def test_all_prints_the_tail_for_when_the_tail_is_the_subject(
        self, conn: sqlite3.Connection, sett: Settings, capsys
    ) -> None:  # type: ignore[no-untyped-def]
        from backglass.__main__ import _echo_overflow

        proposal = self._proposal(conn, sett)
        _echo_overflow(proposal, all_overflow=True)
        printed = capsys.readouterr().err

        assert printed.count("did not fit:") == len(proposal.overflow)
        assert "more did not fit" not in printed

    def test_no_overflow_says_nothing_at_all(
        self, conn: sqlite3.Connection, sett: Settings, capsys
    ) -> None:  # type: ignore[no-untyped-def]
        from backglass.__main__ import _echo_overflow

        _echo_overflow(planner.propose(conn, sett, SUNDAY, events=[]))
        assert capsys.readouterr().err == ""


def test_a_planned_day_leaves_due_now_empty(conn, weekends: Settings) -> None:  # type: ignore[no-untyped-def]
    """`due_now` belongs to the no-plan path. On a day that got planned, the due item is
    a block on the schedule and repeating it as "due" would be noise."""
    add_commitment(conn, weekends, "Move-in: Willow Hall 502", minutes=180, due=SUNDAY)
    proposal = planner.propose(conn, weekends, SUNDAY, events=[])

    assert proposal.capacity.plannable
    assert proposal.due_now == []


# ── all-day plans are visible and cost nothing ─────────────────────────────────
#
# `engagement` #8 in the owner's ledger is "McKenna Summer Program", 2026-08-09 to
# 2026-08-14, confirmed. It rendered on no day at all: `engagement_events` skipped every
# row with a date and no hour, and the rule it was obeying is about not deleting capacity
# on a guess — never about hiding the plan. Six days inside a programme the schedule
# never mentioned.
PROGRAMME_START = date(2026, 8, 9)
PROGRAMME_END = date(2026, 8, 14)


class TestAllDayPlans:
    def test_a_multi_day_plan_appears_on_every_day_it_covers(  # type: ignore[no-untyped-def]
        self, conn, sett: Settings
    ) -> None:
        add_engagement(
            conn,
            what="McKenna Summer Program",
            starts_at=PROGRAMME_START.isoformat(),
            ends_at=PROGRAMME_END.isoformat(),
        )
        seen = {}
        for offset in range((PROGRAMME_END - PROGRAMME_START).days + 1):
            day = PROGRAMME_START + timedelta(days=offset)
            (event,) = capacity_mod.engagement_events(conn, day, PHOENIX)
            seen[day] = event.title

        assert len(seen) == 6
        assert seen[PROGRAMME_START] == "McKenna Summer Program (day 1 of 6)"
        assert seen[PROGRAMME_END] == "McKenna Summer Program (day 6 of 6)"
        assert seen[date(2026, 8, 11)] == "McKenna Summer Program (day 3 of 6)"

    def test_the_day_after_it_ends_is_clear(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        """The window query is inclusive at both ends and stops there — an unbounded one
        would put a finished programme on every day for the rest of the ledger."""
        add_engagement(
            conn,
            what="McKenna Summer Program",
            starts_at=PROGRAMME_START.isoformat(),
            ends_at=PROGRAMME_END.isoformat(),
        )
        after = PROGRAMME_END + timedelta(days=1)
        assert capacity_mod.engagement_events(conn, after, PHOENIX) == []
        before = PROGRAMME_START - timedelta(days=1)
        assert capacity_mod.engagement_events(conn, before, PHOENIX) == []

    def test_an_all_day_plan_spends_no_capacity_on_any_of_its_days(  # type: ignore[no-untyped-def]
        self, conn, weekends: Settings
    ) -> None:
        """The load-bearing assertion. An all-day banner spans midnight to midnight, so
        leaving it in the capacity arithmetic would swallow the entire window and report
        a fully booked day for six days running."""
        # Compared against the same day without the plan, not against zero: the routines
        # (lunch, gym) legitimately sit inside the window and spend their own minutes.
        # What is under test is the delta the banner adds, which must be none of it.
        baseline = {}
        for n in range(6):
            day = PROGRAMME_START + timedelta(days=n)
            cap = capacity_mod.compute(conn, weekends, day)
            baseline[day] = (cap.capacity_minutes, cap.fixed_minutes, cap.buffer_minutes)

        add_engagement(
            conn,
            what="McKenna Summer Program",
            starts_at=PROGRAMME_START.isoformat(),
            ends_at=PROGRAMME_END.isoformat(),
        )
        for day, before in baseline.items():
            cap = capacity_mod.compute(conn, weekends, day)
            assert (cap.capacity_minutes, cap.fixed_minutes, cap.buffer_minutes) == before, day
            assert before[0] > 0, "the day must have had capacity to lose in the first place"

    def test_a_single_day_plan_is_not_numbered(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        """"day 1 of 1" is noise. The numbering earns its place only across a run."""
        add_engagement(conn, what="orientation", starts_at=THURSDAY.isoformat())
        (event,) = capacity_mod.engagement_events(conn, THURSDAY, PHOENIX)
        assert event.title == "orientation"

    def test_a_proposed_all_day_plan_is_still_invisible(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        """The exclusion that had a reason. Someone suggesting a week is not a week, and
        making all-day plans visible must not smuggle proposals onto the day."""
        add_engagement(
            conn,
            what="maybe a retreat",
            starts_at=THURSDAY.isoformat(),
            ends_at=FRIDAY.isoformat(),
            status="proposed",
        )
        assert capacity_mod.engagement_events(conn, THURSDAY, PHOENIX) == []

    def test_the_plan_carries_the_banner_as_a_block(self, conn, weekends: Settings) -> None:  # type: ignore[no-untyped-def]
        """End to end: it reaches `propose`, so it is persisted and every reader sees it."""
        add_engagement(
            conn,
            what="McKenna Summer Program",
            starts_at=PROGRAMME_START.isoformat(),
            ends_at=PROGRAMME_END.isoformat(),
        )
        proposal = planner.propose(conn, weekends, PROGRAMME_START)

        banners = [b for b in proposal.blocks if b["kind"] == "allday"]
        assert [b["title"] for b in banners] == ["McKenna Summer Program (day 1 of 6)"]
        # It is a fact about the day, not time spent in it: it reaches the plan without
        # reaching the arithmetic, so nothing in `capacity.fixed` is the banner.
        assert not any(e.allday for e in proposal.capacity.fixed)
        assert proposal.capacity.plannable


# ── three readings of one dinner ───────────────────────────────────────────────
#
# The owner's plan for 2026-08-09 drew the same meal three times: "McKenna Program
# Welcome Dinner" 18:00–20:00, "McKenna Summer Program kickoff dinner" 18:30–19:30 and
# "college dinner appointment" 18:30–19:30 — engagement rows 195, 183 and 184, three
# extractions of one invitation. `_distinct` cannot help: it collapses exact
# (title, start, end) triples, which is right for two calendars describing one meeting
# and useless here.


class TestNearDuplicateEngagements:
    def test_three_readings_of_one_dinner_render_once(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        add_engagement(
            conn,
            what="McKenna Program Welcome Dinner",
            starts_at=f"{THURSDAY.isoformat()}T18:00:00-07:00",
            ends_at=f"{THURSDAY.isoformat()}T20:00:00-07:00",
        )
        add_engagement(
            conn,
            what="McKenna Summer Program kickoff dinner",
            starts_at=f"{THURSDAY.isoformat()}T18:30:00-07:00",
            ends_at=f"{THURSDAY.isoformat()}T19:30:00-07:00",
        )
        events = capacity_mod.engagement_events(conn, THURSDAY, PHOENIX)
        assert len(events) == 1
        assert "McKenna" in events[0].title

    def test_a_weaker_reading_loses_to_the_one_the_model_believed(  # type: ignore[no-untyped-def]
        self, conn, sett: Settings
    ) -> None:
        """The survivor is chosen by confidence, not by insertion order — the row the
        model was surest of is the one whose hours the day should be built on."""
        conn.execute(
            "INSERT INTO engagement (user_id, kind, what, starts_at, ends_at, "
            " when_is_explicit, location, status, confidence, source_item_id, created_at) "
            "VALUES (?, 'social', 'McKenna Program dinner guess', ?, ?, 1, NULL, "
            " 'confirmed', 0.72, ?, ?)",
            (
                USER_ID,
                f"{THURSDAY.isoformat()}T18:30:00-07:00",
                f"{THURSDAY.isoformat()}T19:30:00-07:00",
                _any_source_item(conn),
                now_iso(),
            ),
        )
        conn.execute(
            "INSERT INTO engagement (user_id, kind, what, starts_at, ends_at, "
            " when_is_explicit, location, status, confidence, source_item_id, created_at) "
            "VALUES (?, 'social', 'McKenna Program Welcome Dinner', ?, ?, 1, NULL, "
            " 'confirmed', 0.94, ?, ?)",
            (
                USER_ID,
                f"{THURSDAY.isoformat()}T18:00:00-07:00",
                f"{THURSDAY.isoformat()}T20:00:00-07:00",
                _any_source_item(conn),
                now_iso(),
            ),
        )
        (event,) = capacity_mod.engagement_events(conn, THURSDAY, PHOENIX)
        assert event.title == "McKenna Program Welcome Dinner"
        assert event.minutes == 120

    def test_a_real_double_booking_is_never_merged(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        """The failure that would cost the most. Two different plans at one hour is a
        conflict the owner has to see; merging it deletes a meeting and says nothing."""
        add_engagement(
            conn,
            what="dentist appointment",
            starts_at=f"{THURSDAY.isoformat()}T14:00:00-07:00",
            ends_at=f"{THURSDAY.isoformat()}T15:00:00-07:00",
        )
        add_engagement(
            conn,
            what="advising call with Abby",
            starts_at=f"{THURSDAY.isoformat()}T14:00:00-07:00",
            ends_at=f"{THURSDAY.isoformat()}T15:00:00-07:00",
        )
        assert len(capacity_mod.engagement_events(conn, THURSDAY, PHOENIX)) == 2

    def test_two_lunches_with_different_people_stay_two(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        """Why the token floor is five characters and not four: "lunch with Sarah" and
        "lunch with Tom" share `lunch` and `with`, and are two different lunches."""
        add_engagement(
            conn,
            what="lunch with Sarah",
            starts_at=f"{THURSDAY.isoformat()}T12:00:00-07:00",
            ends_at=f"{THURSDAY.isoformat()}T13:00:00-07:00",
        )
        add_engagement(
            conn,
            what="lunch with Tom",
            starts_at=f"{THURSDAY.isoformat()}T12:30:00-07:00",
            ends_at=f"{THURSDAY.isoformat()}T13:30:00-07:00",
        )
        assert len(capacity_mod.engagement_events(conn, THURSDAY, PHOENIX)) == 2

    def test_the_same_plan_at_a_different_hour_stays_two(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        """Words alone are not evidence. This week's standup does not absorb next
        Tuesday's, and two sittings of one seminar are two things to be at."""
        add_engagement(
            conn,
            what="McKenna Program seminar",
            starts_at=f"{THURSDAY.isoformat()}T09:00:00-07:00",
            ends_at=f"{THURSDAY.isoformat()}T10:00:00-07:00",
        )
        add_engagement(
            conn,
            what="McKenna Program seminar",
            starts_at=f"{THURSDAY.isoformat()}T15:00:00-07:00",
            ends_at=f"{THURSDAY.isoformat()}T16:00:00-07:00",
        )
        assert len(capacity_mod.engagement_events(conn, THURSDAY, PHOENIX)) == 2

    def test_a_banner_never_swallows_a_timed_plan(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        """An all-day banner spans midnight to midnight, so it overlaps everything on the
        day by construction. Without the same-kind guard the words alone would merge these
        two and a real hour would vanish from the schedule."""
        add_engagement(
            conn,
            what="BioBridge Early Start Program",
            starts_at=THURSDAY.isoformat(),
            ends_at=FRIDAY.isoformat(),
        )
        add_engagement(
            conn,
            what="BioBridge Early Start orientation",
            starts_at=f"{THURSDAY.isoformat()}T09:00:00-07:00",
            ends_at=f"{THURSDAY.isoformat()}T10:00:00-07:00",
        )
        events = capacity_mod.engagement_events(conn, THURSDAY, PHOENIX)
        assert len(events) == 2
        assert sorted(e.allday for e in events) == [False, True]

    def test_nothing_is_written_when_a_duplicate_is_dropped(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        """A merge changes what the day renders, never what the owner can go and look at.
        Both rows stay in the ledger, reachable from the Engagements page."""
        add_engagement(
            conn,
            what="McKenna Program Welcome Dinner",
            starts_at=f"{THURSDAY.isoformat()}T18:00:00-07:00",
            ends_at=f"{THURSDAY.isoformat()}T20:00:00-07:00",
        )
        add_engagement(
            conn,
            what="McKenna Summer Program kickoff dinner",
            starts_at=f"{THURSDAY.isoformat()}T18:30:00-07:00",
            ends_at=f"{THURSDAY.isoformat()}T19:30:00-07:00",
        )
        capacity_mod.engagement_events(conn, THURSDAY, PHOENIX)
        assert conn.execute("SELECT COUNT(*) AS n FROM engagement").fetchone()["n"] == 2

    def test_the_day_is_charged_for_one_dinner_not_three(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        """The point of all of it. Three readings of one 90-minute meal inside the window
        used to subtract three meals' worth of capacity from an evening that held one."""
        evening = sett.model_copy(update={"working_window": "09:00-22:00"})
        alone = capacity_mod.compute(conn, evening, THURSDAY).capacity_minutes
        add_engagement(
            conn,
            what="McKenna Program Welcome Dinner",
            starts_at=f"{THURSDAY.isoformat()}T18:00:00-07:00",
            ends_at=f"{THURSDAY.isoformat()}T20:00:00-07:00",
        )
        one = capacity_mod.compute(conn, evening, THURSDAY).capacity_minutes
        add_engagement(
            conn,
            what="McKenna Summer Program kickoff dinner",
            starts_at=f"{THURSDAY.isoformat()}T18:30:00-07:00",
            ends_at=f"{THURSDAY.isoformat()}T19:30:00-07:00",
        )
        still_one = capacity_mod.compute(conn, evening, THURSDAY).capacity_minutes

        assert one < alone, "the dinner must cost the evening something"
        assert still_one == one, "the second reading of it must cost nothing more"


class TestDedupDefectsFoundInReview:
    """Six cases an adversarial review produced that the first pass missed.

    Every one is a silent deletion: an event the owner has, that the day stops showing,
    with nothing anywhere saying it was dropped. That is the failure mode this collapse
    is most dangerous for, so each gets its own name.
    """

    VENUE = "Armstrong Hall Rotunda, 1100 S McAllister Ave, Tempe, AZ 85281"

    def test_one_venue_does_not_merge_the_meetings_held_in_it(  # type: ignore[no-untyped-def]
        self, conn, sett: Settings
    ) -> None:
        """`engagement_events` appends " — {location}" to the title before the collapse
        runs, so a shared address used to supply every token identity needed. Two
        unrelated plans in one campus building overlapped, shared five long words of
        street address, and one of them disappeared — taking its minutes out of the
        capacity subtraction too, so the day reported MORE free time than it had."""
        add_engagement(
            conn,
            what="Law school info session",
            starts_at=f"{THURSDAY.isoformat()}T14:00:00-07:00",
            ends_at=f"{THURSDAY.isoformat()}T15:00:00-07:00",
            location=self.VENUE,
        )
        add_engagement(
            conn,
            what="Barrett honors reception",
            starts_at=f"{THURSDAY.isoformat()}T14:30:00-07:00",
            ends_at=f"{THURSDAY.isoformat()}T16:00:00-07:00",
            location=self.VENUE,
        )
        events = capacity_mod.engagement_events(conn, THURSDAY, PHOENIX)
        assert len(events) == 2, "a room is not evidence that two meetings are one"
        assert {e.title.split(" — ")[0] for e in events} == {
            "Law school info session",
            "Barrett honors reception",
        }

    def test_two_different_programmes_do_not_annihilate_each_other(  # type: ignore[no-untyped-def]
        self, conn, sett: Settings
    ) -> None:
        """All-day banners span midnight to midnight, so overlap is guaranteed and
        carries no information. Matched on it, a six-day programme and a three-day one
        sharing two words deleted each other on alternate days — leaving the longer one
        with a hole in its middle and the winner flipping day to day."""
        add_engagement(
            conn,
            what="McKenna Summer Program",
            starts_at="2026-08-09",
            ends_at="2026-08-14",
        )
        add_engagement(
            conn,
            what="McKenna Research Program",
            starts_at="2026-08-10",
            ends_at="2026-08-12",
        )
        for day in (date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12)):
            titles = {
                e.title.split(" (")[0]
                for e in capacity_mod.engagement_events(conn, day, PHOENIX)
            }
            assert titles == {"McKenna Summer Program", "McKenna Research Program"}, day
        # And the longer one still runs its whole length, unbroken.
        for offset in range(6):
            day = date(2026, 8, 9) + timedelta(days=offset)
            assert any(
                e.title.startswith("McKenna Summer Program")
                for e in capacity_mod.engagement_events(conn, day, PHOENIX)
            ), day

    def test_two_readings_of_one_programme_still_collapse(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        """The fix above must not cost the case it was built for: two extractions of one
        programme name the same run of days, and are one banner."""
        add_engagement(
            conn,
            what="BioBridge 2026 Early Start Program",
            starts_at="2026-08-05",
            ends_at="2026-08-15",
        )
        add_engagement(
            conn,
            what="Biomedical Sciences Early Start program",
            starts_at="2026-08-05",
            ends_at="2026-08-15",
        )
        events = capacity_mod.engagement_events(conn, date(2026, 8, 9), PHOENIX)
        assert len(events) == 1

    def test_a_run_of_days_written_with_a_clock_is_still_a_run_of_days(  # type: ignore[no-untyped-def]
        self, conn, sett: Settings
    ) -> None:
        """Read literally, "2026-08-10T09:00" to "2026-08-14" is a single 5220-minute
        event: it swallowed the whole of the 10th's window — capacity zero — and then
        appeared on none of the four days after it."""
        add_engagement(
            conn,
            what="BioBridge Early Start",
            starts_at="2026-08-10T09:00:00-07:00",
            ends_at="2026-08-14",
        )
        for offset in range(5):
            day = date(2026, 8, 10) + timedelta(days=offset)
            (event,) = capacity_mod.engagement_events(conn, day, PHOENIX)
            assert event.allday is True, day
            assert event.minutes == 1440, day

        weekday = date(2026, 8, 11)  # a Tuesday, inside the default working days
        before = capacity_mod.compute(conn, sett, weekday).capacity_minutes
        assert before > 0, "a five-day programme must not zero the days it spans"

    def test_a_degenerate_window_is_refused_rather_than_reported_two_ways(  # type: ignore[no-untyped-def]
        self, sett: Settings
    ) -> None:
        """"09:00-09:00" parsed, made `window_minutes` zero on a day `is_working_day`
        called a working day, and the planner then said "not a working day" while the
        schedule page said "fully booked" about the same Thursday."""
        import pydantic

        with pytest.raises(pydantic.ValidationError, match="ends at or before it starts"):
            Settings(owner_name="K", db_path=sett.db_path, working_window="09:00-09:00")
        with pytest.raises(pydantic.ValidationError, match="ends at or before it starts"):
            Settings(owner_name="K", db_path=sett.db_path, weekend_window="18:00-10:00")


class TestAnHourTwoThingsCoverIsOneHour:
    """Capacity summed each fixed event's length instead of unioning them.

    Found on the owner's own semester, which starts 2026-08-20 and overlaps constantly:
    CIS 236 09:00–10:15 against BIO 181 10:30–11:45 against CHM 113 (Lab) 10:00–11:50 on
    one Wednesday. Reported 545 minutes of fixed time where the union is 415, so the
    planner believed an 80-minute day was all that remained of one holding three and a
    half free hours.

    It failed quietly, which is why it survived: a short day looks like a busy day, not
    like a bug, and P3's "under 60 minutes, do not propose a plan" would eventually have
    declined a perfectly plannable Wednesday. `_free_slots` had always merged before
    measuring, so the two halves of the capacity formula disagreed exactly when events
    overlapped — and `min` took the wrong one every time.
    """

    def test_two_overlapping_meetings_cost_their_union_not_their_sum(
        self, conn, sett: Settings
    ) -> None:  # type: ignore[no-untyped-def]
        both = capacity_mod.compute(
            conn, sett, THURSDAY,
            events=[meeting(THURSDAY, "10:00", "11:00"), meeting(THURSDAY, "10:30", "11:30")],
        )
        one = capacity_mod.compute(
            conn, sett, THURSDAY, events=[meeting(THURSDAY, "10:00", "11:30")],
        )
        # 10:00–11:30 covered either way. The sum would have said 120 minutes.
        assert both.fixed_minutes == 90
        assert both.fixed_minutes == one.fixed_minutes
        assert both.capacity_minutes == one.capacity_minutes

    def test_a_three_way_overlap_is_still_one_span(
        self, conn, sett: Settings
    ) -> None:  # type: ignore[no-untyped-def]
        """The Wednesday shape. Summing gives 205 minutes for 110 minutes of clock."""
        cap = capacity_mod.compute(
            conn, sett, THURSDAY,
            events=[
                meeting(THURSDAY, "10:00", "11:15", "CIS 236"),
                meeting(THURSDAY, "10:00", "11:50", "CHM 113 (Lab)"),
                meeting(THURSDAY, "10:30", "11:45", "BIO 181"),
            ],
        )
        assert cap.fixed_minutes == 110

    def test_back_to_back_meetings_are_unchanged(
        self, conn, sett: Settings
    ) -> None:  # type: ignore[no-untyped-def]
        """Touching is not overlapping. The fix must not quietly discount a real day."""
        cap = capacity_mod.compute(
            conn, sett, THURSDAY,
            events=[meeting(THURSDAY, "10:00", "11:00"), meeting(THURSDAY, "11:00", "12:00")],
        )
        assert cap.fixed_minutes == 120

    def test_the_reported_parts_add_up_to_the_time_actually_occupied(
        self, conn, sett: Settings
    ) -> None:  # type: ignore[no-untyped-def]
        """The capacity line is read as arithmetic — "480m of 720m (fixed 415m, buffer
        50m…)" — so the parts have to be disjoint. Buffer is now what the buffers added
        beyond the events, which is what a reader means by it: a buffer that lands inside
        the next meeting costs nothing and says so."""
        cap = capacity_mod.compute(
            conn, sett, THURSDAY,
            events=[
                meeting(THURSDAY, "10:00", "11:00"),
                meeting(THURSDAY, "10:30", "11:30"),
                meeting(THURSDAY, "14:00", "15:00", travel=True),
            ],
        )
        occupied = cap.window_minutes - (cap.capacity_minutes + cap.reserve_minutes
                                         + cap.review_minutes)
        assert cap.fixed_minutes + cap.buffer_minutes + cap.travel_minutes <= occupied
        assert cap.buffer_minutes >= 0
        assert cap.fixed_minutes == 90 and cap.travel_minutes == 60

    def test_travel_and_a_meeting_over_one_minute_charge_it_once(
        self, conn, sett: Settings
    ) -> None:  # type: ignore[no-untyped-def]
        """Attributed to travel, so the two categories stay disjoint rather than both
        claiming the overlap and reinstating the bug inside the breakdown."""
        cap = capacity_mod.compute(
            conn, sett, THURSDAY,
            events=[
                meeting(THURSDAY, "10:00", "11:00", travel=True),
                meeting(THURSDAY, "10:30", "11:30"),
            ],
        )
        assert cap.travel_minutes == 60
        assert cap.fixed_minutes == 30


# ── goal 4 increment C: honest estimates need a sitting, not a wall ───────────
#
# `coursework` now reads real numbers off the assignment — four exams at two hours, five
# CIS 236 milestones between 138 and 344 minutes. Handed to the planner whole, every one
# of those is unschedulable: `select` drops anything larger than the capacity left and
# `place` needs a contiguous slot that long. Honest estimates without a clamp make the
# biggest work vanish from every plan, which is strictly worse than the flat thirty
# minutes they replaced. These hold that line, and the one behind it: a sitting is not
# the obligation, so finishing one must not close it.


def _a_day_with_the_protected_slot_already_taken(conn, sett: Settings) -> int:  # type: ignore[no-untyped-def]
    """Something due today ahead of the big thing, so the protected block is spoken for.

    Load-bearing: the protected slot is a fixed 90 minutes and takes the first real piece
    of work, so with the milestone at the head of the queue it would be truncated there
    and the clamp would never run. A test that passes with the clamp removed is not a
    test of the clamp — mutation-checked by replacing `sitting()` with `remaining`.
    """
    add_commitment(conn, sett, "reply to Dana", minutes=60, due=THURSDAY, n=1)
    commitment_id = add_commitment(
        conn, sett, "T - Final Analysis, RFP, and Presentation", minutes=344, n=2
    )
    # What makes it divisible: `coursework` read 344 minutes off the assignment's own
    # page limit. A number a person typed onto a move-in would not be.
    conn.execute(
        "UPDATE commitment SET estimate_source = 'analyzed' WHERE id = ?", (commitment_id,)
    )
    return commitment_id


def test_work_larger_than_a_sitting_is_still_scheduled(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    commitment_id = _a_day_with_the_protected_slot_already_taken(conn, sett)

    proposal = planner.propose(conn, sett, THURSDAY, events=[])

    mine = [b for b in proposal.blocks if b["commitment_id"] == commitment_id]
    assert mine, "a 344-minute deliverable must not fall off the plan entirely"
    assert mine[0]["minutes"] == sett.max_block_minutes
    assert [c.commitment_id for c in proposal.overflow] == []


def test_a_clamped_block_says_it_is_only_part_of_the_work(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """A block reading "T - Final Analysis" for 90 minutes of a 344-minute deliverable
    would be a quiet lie about what finishing it means, and the owner marks these done by
    reading them."""
    commitment_id = _a_day_with_the_protected_slot_already_taken(conn, sett)

    proposal = planner.propose(conn, sett, THURSDAY, events=[])

    title = next(
        b["title"] for b in proposal.blocks if b["commitment_id"] == commitment_id
    )
    assert "90m of 344m left" in title


def test_work_that_fits_is_not_split(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """Not every long obligation is divisible — "Move-in: Willow Hall 502" is three hours
    of one thing. The split is what happens instead of the item disappearing, never
    instead of it being planned properly."""
    add_commitment(conn, sett, "reply to Dana", minutes=60, due=THURSDAY, n=1)
    commitment_id = add_commitment(conn, sett, "Move-in: Willow Hall 502", minutes=180, n=2)

    proposal = planner.propose(conn, sett, THURSDAY, events=[])

    mine = [b for b in proposal.blocks if b["commitment_id"] == commitment_id]
    assert [b["minutes"] for b in mine] == [180]
    assert mine[0]["title"] == "Move-in: Willow Hall 502"


def test_a_sitting_that_is_done_shrinks_what_is_left(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """Otherwise a multi-session assignment keeps its whole estimate every morning and is
    scheduled forever."""
    commitment_id = add_commitment(conn, sett, "T - Final Analysis", minutes=344)
    conn.execute(
        "INSERT INTO day_plan (user_id, local_date, tz, capacity_minutes, generated_at) "
        "VALUES (?, '2026-07-29', ?, 480, ?)",
        (USER_ID, PHOENIX, now_iso()),
    )
    plan_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, commitment_id, "
        " title, outcome) VALUES (?, ?, ?, 'work', ?, 'T - Final Analysis', 'done')",
        (plan_id, at(MONDAY, "09:00").isoformat(), at(MONDAY, "10:30").isoformat(),
         commitment_id),
    )

    pool = planner.candidates(conn, sett, THURSDAY, set())
    item = next(c for c in pool if c.commitment_id == commitment_id)
    assert item.done_minutes == 90
    assert item.remaining == 254


def test_one_sitting_done_does_not_close_multi_session_work(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """Increment 7 made a done block resolve the commitment behind it, which is right for
    the single-sitting work that is nearly all of the ledger. With a clamp it would delete
    three days of a CIS 236 milestone on the first click."""
    from backglass.web import actions

    commitment_id = add_commitment(conn, sett, "T - Final Analysis", minutes=344)
    proposal = planner.propose(conn, sett, THURSDAY, events=[])
    plan_id = planner.persist(conn, sett, proposal)
    block_id = int(
        conn.execute(
            "SELECT id FROM plan_block WHERE day_plan_id = ? "
            "AND commitment_id IS NOT NULL", (plan_id,)
        ).fetchone()["id"]
    )

    result = actions.set_block_outcome(conn, block_id, "done")

    assert result.detail == "progress recorded"
    status = conn.execute(
        "SELECT status FROM commitment WHERE id = ?", (commitment_id,)
    ).fetchone()["status"]
    assert status == "open"


def test_the_last_sitting_does_close_it(conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
    """And single-sitting work still resolves on the click exactly as it did, because one
    sitting is the whole estimate."""
    from backglass.web import actions

    commitment_id = add_commitment(conn, sett, "reply to Dana", minutes=45)
    proposal = planner.propose(conn, sett, THURSDAY, events=[])
    plan_id = planner.persist(conn, sett, proposal)
    block_id = int(
        conn.execute(
            "SELECT id FROM plan_block WHERE day_plan_id = ? "
            "AND commitment_id IS NOT NULL", (plan_id,)
        ).fetchone()["id"]
    )

    actions.set_block_outcome(conn, block_id, "done")

    status = conn.execute(
        "SELECT status FROM commitment WHERE id = ?", (commitment_id,)
    ).fetchone()["status"]
    assert status == "done"
