"""docs/04 §5 acceptance criteria, plus the requirements they rest on.

The nine bullets in §5 are the exit criterion for Phase 4 (docs/09), so each one has a
test named after it. Everything else here exists because a §5 bullet cannot be trusted
without it — a capacity model that happens to produce the right total by cancelling two
errors would pass the first bullet and fail in week two.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
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
        "VALUES (?, 'i_owe', ?, ?, ?, ?, 'manual', 0.9, 'open', ?, ?, ?, ?)",
        (
            USER_ID,
            ledger.resolve_entity("Dana <dana@example.gov>"),
            what,
            due.isoformat() if due else None,
            minutes,
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

    # Arrives overnight, and is due sooner — so it outranks on every tier except the one
    # that matters here. P10 puts rollover above newly selected work regardless.
    add_commitment(conn, sett, "brand new work", minutes=60, n=2, due=FRIDAY)

    tomorrow = planner.propose(conn, sett, FRIDAY, events=[])
    work = [b for b in tomorrow.blocks if b["kind"] in ("protected", "work")]
    assert work
    assert "yesterday's work" in str(work[0]["title"]), (
        "P10: rollover items appear above newly selected work"
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
    """ "review 30, draft 60, decision 15, meeting-prep 30, unknown 45"."""
    table = estimates.defaults(sett)
    assert table == {
        "review": 30,
        "draft": 60,
        "decision": 15,
        "meeting_prep": 30,
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
    writes nothing."""
    import inspect

    source = inspect.getsource(estimates.ratio_report)
    assert "UPDATE" not in source.upper()
    report = estimates.ratio_report(conn)
    assert not report.ready
    assert report.sentence() is None


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
