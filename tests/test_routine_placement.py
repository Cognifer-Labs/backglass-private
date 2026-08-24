"""Routines are placed around the day, and a day without homework still holds study time.

docs/04 §1.9, rules P17–P20.

The parser, the 12-hour clock and the schedule rendering live in `test_routines.py`;
this file is only about *where on the day* a routine lands, and what fills a day that
holds no homework.

Owner's ruling, 2026-08-21: *"everything should be planned around my schedule and fixed
events; breakfast, lunch, dinner and gym should be planned around this, along with some
amount of relaxation time and study time and homework time."*

The bug that prompted it is in the first test. The live plan for Monday 2026-08-24 read

    12:20pm–1:10pm  CHM 113 [fixed]
    12:30pm–1:15pm  Lunch [routine]

which is not a scheduling conflict so much as a plan that is wrong about when the owner
eats. `parse_routines` emitted a span at a decreed hour and `routine_events` stamped it
on the day whatever else was there.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID
from backglass.plan import capacity as capacity_mod
from backglass.plan import planner, rollover
from backglass.plan.capacity import FixedEvent

PHOENIX = "America/Phoenix"
KOLKATA = "Asia/Kolkata"
#: A Monday, the shape of the day the owner complained about.
MONDAY = date(2026, 8, 24)


@pytest.fixture
def sett(settings: Settings) -> Settings:
    return settings.model_copy(update={"default_tz": PHOENIX, "confidence_threshold": 0.7})


def at(day: date, hhmm: str, tz: str = PHOENIX) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(tz))


def klass(day: date, start: str, end: str, title: str = "CHM 113", tz: str = PHOENIX):  # type: ignore[no-untyped-def]
    return FixedEvent(starts_at=at(day, start, tz), ends_at=at(day, end, tz), title=title)


def routines(sett: Settings, spec: str) -> Settings:
    return sett.model_copy(update={"routines": spec})


def placed(sett: Settings, day: date, around: list[FixedEvent], tz: str = PHOENIX):  # type: ignore[no-untyped-def]
    return {e.title: e for e in capacity_mod.routine_events(sett, day, tz, around=around)}


class TestRoutinesArePlacedAroundTheDay:
    def test_lunch_moves_out_of_the_class_it_was_sitting_in(self, sett: Settings) -> None:
        """The reported bug, as the owner saw it."""
        sett = routines(sett, "lunch@12:30+45")

        lunch = placed(sett, MONDAY, [klass(MONDAY, "12:20", "13:10")])["Lunch"]

        assert lunch.starts_at == at(MONDAY, "13:10"), "lunch waits for the class to end"
        assert lunch.ends_at == at(MONDAY, "13:55")
        assert not lunch.conflict

    def test_an_hour_nothing_touches_is_left_exactly_where_it_was(
        self, sett: Settings
    ) -> None:
        """Flexible is not restless. A free preferred hour drifts by zero minutes —
        which is also what keeps the placement stable enough to hash (0028)."""
        sett = routines(sett, "lunch@12:30+45")

        lunch = placed(sett, MONDAY, [klass(MONDAY, "09:00", "10:15", "CIS 236")])["Lunch"]

        assert lunch.starts_at == at(MONDAY, "12:30")

    def test_of_two_equally_distant_gaps_the_earlier_one_wins(self, sett: Settings) -> None:
        """A 60-minute class centred on the preferred hour leaves a gap 30 minutes before
        and a gap 30 minutes after. Eating before the class beats eating after it, and
        either beats a coin toss — a tie broken by iteration order would place lunch
        somewhere different on two runs over the same day."""
        sett = routines(sett, "lunch@12:30+30")

        lunch = placed(sett, MONDAY, [klass(MONDAY, "12:15", "13:15")])["Lunch"]

        assert lunch.starts_at == at(MONDAY, "11:45")

    def test_a_day_with_no_room_keeps_the_hour_and_names_what_it_hit(
        self, sett: Settings
    ) -> None:
        """Never silently dropped, never silently moved out of the day. P2's rule one
        register down: a meal the planner could not place is a fact about a day too full
        to eat in, and the owner can act on it."""
        sett = routines(sett, "lunch@12:30+45")
        wall = [
            klass(MONDAY, "10:00", "13:00", "BIO 181"),
            klass(MONDAY, "13:00", "15:00", "LSB 191"),
        ]

        lunch = placed(sett, MONDAY, wall)["Lunch"]

        assert lunch.starts_at == at(MONDAY, "12:30"), "it keeps its hour"
        assert "BIO 181" in lunch.conflict and "no free 45m gap" in lunch.conflict

    def test_the_drift_is_bounded_so_lunch_never_becomes_dinner(
        self, sett: Settings
    ) -> None:
        """A free gap exists at 15:00 — three hours out. Past the bound the routine keeps
        its hour and says so, because a meal moved that far is not the meal."""
        sett = routines(sett, "lunch@12:30+45")
        wall = [klass(MONDAY, "10:00", "15:00", "BIO 181 (Lab)")]

        lunch = placed(sett, MONDAY, wall)["Lunch"]

        assert lunch.starts_at == at(MONDAY, "12:30")
        assert lunch.conflict

    def test_a_pinned_routine_never_moves(self, sett: Settings) -> None:
        """`banner@16:00+240!@wed` is volunteering somebody else scheduled. A planner that
        quietly moved it to 5pm would be inventing an appointment."""
        sett = routines(sett, "banner@16:00+240!")

        banner = placed(sett, MONDAY, [klass(MONDAY, "16:30", "17:00", "Advising")])["Banner"]

        assert banner.starts_at == at(MONDAY, "16:00")
        assert not banner.conflict, "a pin is a decision, not a collision to report"

    def test_a_flexible_routine_yields_to_a_pinned_one(self, sett: Settings) -> None:
        """Which of the two moves is decided by the pin, not by the order of the config
        line — so gym written first does not win over volunteering written second."""
        sett = routines(sett, "gym@16:00+60,banner@16:00+120!")

        events = placed(sett, MONDAY, [])

        assert events["Banner"].starts_at == at(MONDAY, "16:00")
        # Before it, not after: the hour ending as the pin begins is 60 minutes from the
        # preferred start and the hour after the pin is 120.
        assert events["Gym"].starts_at == at(MONDAY, "15:00")

    def test_routines_do_not_land_on_each_other(self, sett: Settings) -> None:
        """Each placed routine joins the obstacle set, so a shifted breakfast cannot be
        shifted onto lunch."""
        sett = routines(sett, "breakfast@07:30+30,lunch@08:00+45")

        events = placed(sett, MONDAY, [klass(MONDAY, "07:00", "08:00", "Early lab")])

        assert events["Breakfast"].starts_at == at(MONDAY, "08:00")
        assert events["Lunch"].starts_at == at(MONDAY, "08:30")

    def test_an_all_day_banner_moves_nothing(self, sett: Settings) -> None:
        """An all-day event covers every minute of the day. Treated as an obstacle it
        would push every routine off the day entirely — the same trap `compute` already
        avoids when it drops all-day events before subtracting time."""
        sett = routines(sett, "lunch@12:30+45")
        banner = FixedEvent(
            starts_at=at(MONDAY, "00:00"),
            ends_at=at(MONDAY, "00:00") + timedelta(days=1),
            title="First day of LSB 191",
            kind="allday",
        )

        assert placed(sett, MONDAY, [banner])["Lunch"].starts_at == at(MONDAY, "12:30")

    def test_the_day_scope_still_filters(self, sett: Settings) -> None:
        """The pin marker sits between the duration and the day scope; both still parse."""
        sett = routines(sett, "banner@16:00+240!@wed")

        assert placed(sett, MONDAY, []) == {}
        assert "Banner" in placed(sett, date(2026, 8, 26), [])

    def test_placement_holds_in_the_owners_other_timezone(self, sett: Settings) -> None:
        """The owner moves between UTC-7 and UTC+05:30 (the 2026-07-30 lessons). A shift
        is arithmetic on local wall clocks, so it must be computed in the zone the day is
        lived in, not the one the config was written in."""
        sett = routines(sett, "lunch@12:30+45").model_copy(update={"default_tz": KOLKATA})
        wall = [klass(MONDAY, "12:20", "13:10", "CHM 113", tz=KOLKATA)]

        lunch = placed(sett, MONDAY, wall, tz=KOLKATA)["Lunch"]

        assert lunch.starts_at == at(MONDAY, "13:10", KOLKATA)
        assert lunch.starts_at.tzinfo is not None
        assert lunch.starts_at.utcoffset() == timedelta(hours=5, minutes=30)

    def test_the_same_day_is_placed_the_same_way_twice(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        """Rule 3, and 0028. Two runs over one unchanged day must agree to the minute, or
        the replanner reads its own arithmetic as drift and supersedes a live plan every
        sync."""
        sett = routines(sett, "breakfast@07:30+30,lunch@12:30+45,gym@17:30+60")
        _add_class(conn, MONDAY, "12:20", "13:10", "CHM 113")

        first = capacity_mod.day_events(conn, sett, MONDAY)
        second = capacity_mod.day_events(conn, sett, MONDAY)

        assert [(e.title, e.starts_at) for e in first] == [
            (e.title, e.starts_at) for e in second
        ]
        assert planner.inputs_fingerprint(conn, sett, MONDAY) == planner.inputs_fingerprint(
            conn, sett, MONDAY
        )

    def test_a_shifted_lunch_is_the_lunch_the_whole_app_sees(
        self, conn, sett: Settings  # type: ignore[no-untyped-def]
    ) -> None:
        """Placement lives in `day_events`, the one builder capacity, the persisted plan
        and the schedule page all read — so they cannot disagree about when lunch is."""
        sett = routines(sett, "lunch@12:30+45")
        _add_class(conn, MONDAY, "12:20", "13:10", "CHM 113")

        proposal = planner.propose(conn, sett, MONDAY)

        lunch = next(b for b in proposal.blocks if b["title"] == "Lunch")
        assert lunch["starts_at"] == at(MONDAY, "13:10").isoformat()
        for block in proposal.blocks:
            if block["title"] == "Lunch":
                continue
            start = datetime.fromisoformat(str(block["starts_at"]))
            end = datetime.fromisoformat(str(block["ends_at"]))
            if str(block["kind"]) == "allday":
                continue
            assert end <= at(MONDAY, "13:10") or start >= at(MONDAY, "13:55"), block

    def test_a_routine_the_planner_could_not_place_is_said_out_loud(
        self, conn, sett: Settings  # type: ignore[no-untyped-def]
    ) -> None:
        sett = routines(sett, "lunch@12:30+45")
        _add_class(conn, MONDAY, "10:00", "13:00", "BIO 181")
        _add_class(conn, MONDAY, "13:00", "15:00", "LSB 191")

        proposal = planner.propose(conn, sett, MONDAY)

        assert any("no free 45m gap" in note for note in proposal.notes), proposal.notes

    def test_an_evening_routine_still_spends_no_capacity(
        self, conn, sett: Settings  # type: ignore[no-untyped-def]
    ) -> None:
        """The 21:00 relaxation block against a window that closes at 18:00: it belongs on
        the plan and it costs the working day nothing, exactly as breakfast at 07:30 does.
        This is what makes the hard stop on work the window rather than new machinery."""
        bare = routines(sett, "")
        before = capacity_mod.compute(conn, bare, MONDAY).capacity_minutes

        with_relax = routines(sett, "relax@21:00+120")
        after = capacity_mod.compute(conn, with_relax, MONDAY)

        assert after.capacity_minutes == before
        assert "Relax" in {e.title for e in capacity_mod.day_events(conn, with_relax, MONDAY)}


    def test_the_owner_reads_it_in_the_morning_brief_too(
        self, conn, sett: Settings  # type: ignore[no-untyped-def]
    ) -> None:
        """P18 on the surface that matters. The CLI's proposal notes are in-memory and
        the brief rebuilds every line from the ledger, so a conflict stated only on the
        `Proposal` reaches nobody at six in the morning."""
        from backglass.brief import daily

        sett = routines(sett, "lunch@12:30+45")
        _add_class(conn, MONDAY, "10:00", "13:00", "BIO 181")
        _add_class(conn, MONDAY, "13:00", "15:00", "LSB 191")
        planner.persist(conn, sett, planner.propose(conn, sett, MONDAY))

        brief = daily.build(conn, sett, MONDAY)

        said = [line for line in brief.all_lines() if "no free 45m gap" in line.text]
        assert len(said) == 1, [line.text for line in brief.all_lines()]
        assert said[0].provenance is not None, "CLAUDE.md rule 1"


class TestStudyTime:
    """The owner's answer: study is not a second name for homework. Homework is already
    scheduled by name — a coursework commitment with an estimate read off the assignment
    — so a study block is what a day *without* any of that should still contain."""

    def test_a_day_with_no_coursework_gets_one(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        _add_commitment(conn, sett, "reply to the landlord", minutes=30)

        proposal = planner.propose(conn, sett, MONDAY, events=[])

        study = [b for b in proposal.blocks if b["kind"] == "study"]
        assert len(study) == 1
        assert study[0]["minutes"] == sett.study_block_minutes
        assert study[0]["commitment_id"] is None, "a block, not an obligation"

    def test_a_day_that_already_holds_homework_does_not(
        self, conn, sett: Settings  # type: ignore[no-untyped-def]
    ) -> None:
        _add_commitment(conn, sett, "CIS 236 milestone 2", minutes=60, source="analyzed")

        proposal = planner.propose(conn, sett, MONDAY, events=[])

        # The only piece of real work takes the protected slot (P8), which is still the
        # day holding homework — the study block exists for the day that holds none.
        assert [b for b in proposal.blocks if b["kind"] in ("work", "protected")]
        assert not [b for b in proposal.blocks if b["kind"] == "study"]

    def test_it_never_spends_capacity_the_day_does_not_have(
        self, conn, sett: Settings  # type: ignore[no-untyped-def]
    ) -> None:
        """P1 holds for the synthetic block too: what is left after the real work, never
        more. Five 60-minute obligations against a 9-to-18 window leave under 90 minutes,
        so the study block shrinks rather than overrunning the day."""
        for index in range(5):
            _add_commitment(conn, sett, f"errand {index}", minutes=60, n=index + 1)

        proposal = planner.propose(conn, sett, MONDAY, events=[])

        assert proposal.planned_minutes <= proposal.capacity.capacity_minutes

    def test_switching_it_off_is_one_number(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        proposal = planner.propose(
            conn, sett.model_copy(update={"study_block_minutes": 0}), MONDAY, events=[]
        )

        assert not [b for b in proposal.blocks if b["kind"] == "study"]

    def test_it_is_never_rolled_into_tomorrow(self, conn, sett: Settings) -> None:  # type: ignore[no-untyped-def]
        """An hour of reading nobody did is not a debt. Rolled, it would arrive tomorrow
        as an obligation the owner never made, and P11 would eventually ask them whether
        to drop it — a question about a commitment that does not exist."""
        planner.persist(conn, sett, planner.propose(conn, sett, MONDAY, events=[]))

        report = rollover.close_day(conn, sett, MONDAY, done_block_ids=set())

        assert [b["kind"] for b in _blocks(conn, MONDAY)] == ["study"]
        assert report.rolled == 0


def _blocks(conn, day: date):  # type: ignore[no-untyped-def]
    return conn.execute(
        "SELECT b.kind, b.outcome FROM plan_block b JOIN day_plan p ON p.id = b.day_plan_id "
        "WHERE p.local_date = ? ORDER BY b.starts_at",
        (day.isoformat(),),
    ).fetchall()


def _add_class(conn, day: date, start: str, end: str, title: str) -> None:  # type: ignore[no-untyped-def]
    import json

    starts = at(day, start).isoformat()
    ends = at(day, end).isoformat()
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, raw_json, content_hash) VALUES (?, 'calendar:asu', ?, ?, ?, ?, ?, ?)",
        (
            USER_ID,
            f"cal-{title}-{start}",
            now_iso(),
            starts,
            title,
            json.dumps(
                {"starts_at": starts, "ends_at": ends, "status": "confirmed",
                 "declined": False, "travel": False}
            ),
            f"h-{title}-{start}",
        ),
    )


def _add_commitment(
    conn,  # type: ignore[no-untyped-def]
    sett: Settings,
    what: str,
    *,
    minutes: int = 60,
    n: int = 1,
    source: str = "manual",
) -> None:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash, triage_verdict) "
        "VALUES (?, 'gmail:personal', ?, ?, '2026-08-20T09:00:00-07:00',"
        " 'Dana <dana@example.gov>', ?, 'b', '{}', ?, 'keep')",
        (USER_ID, f"s{n}-{what[:8]}", now_iso(), what, f"h{n}-{what[:8]}"),
    )
    source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, estimated_minutes,"
        " estimate_source, confidence, status, source_item_id, created_at)"
        " VALUES (?, 'i_owe', ?, NULL, ?, ?, 0.9, 'open', ?, ?)",
        (USER_ID, what, minutes, source, source_id, now_iso()),
    )
