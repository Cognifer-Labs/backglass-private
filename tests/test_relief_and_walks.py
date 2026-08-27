"""Two rulings from 2026-08-27, and the floors that keep them safe.

    "if things dont fit remove relax time and mcat study time or reduce time for
     other things"
    "also account for travel and walk to classes"

The walk half is arithmetic the day was getting wrong: `capacity` has subtracted travel
since it was written and nothing ever produced any, so four classes in four buildings
cost zero minutes of walking and the gaps between them were offered to the planner as
time to sit down and work in.

The relief half is a policy change with teeth — it spends the owner's evening — so most
of what is here is about when it must *not* fire and what it must never touch.
"""

from __future__ import annotations

from datetime import date

import pytest

from backglass.config import Settings
from backglass.plan import capacity as capacity_mod
from backglass.plan import planner
from tests.test_planner import PHOENIX, add_commitment, at

MONDAY = date(2026, 8, 31)


@pytest.fixture
def sett(settings: Settings) -> Settings:
    # The routines are pinned here rather than inherited: the shared fixture carries none,
    # and relief is entirely about which of them yield. A test that asserts Relax gave up
    # its evening must be run against a day that actually has a Relax in it.
    return settings.model_copy(
        update={
            "default_tz": PHOENIX,
            "confidence_threshold": 0.7,
            "routines": (
                "breakfast@07:30+30,lunch@12:30+45,gym@17:30+60,"
                "shower@18:35+25,dinner@19:15+45,relax@21:00+120"
            ),
        }
    )


def klass(day: date, start: str, end: str, title: str, room: str):  # type: ignore[no-untyped-def]
    return capacity_mod.FixedEvent(
        starts_at=at(day, start), ends_at=at(day, end), title=title, location=room
    )


def routine(day: date, start: str, end: str, title: str):  # type: ignore[no-untyped-def]
    return capacity_mod.FixedEvent(
        starts_at=at(day, start), ends_at=at(day, end), title=title, kind="routine"
    )


# ── walking between classes ───────────────────────────────────────────────


def test_two_classes_in_two_buildings_cost_a_walk(sett) -> None:  # type: ignore[no-untyped-def]
    events = [
        klass(MONDAY, "09:00", "10:15", "CIS 236", "Tempe BA 396"),
        klass(MONDAY, "10:30", "11:45", "BIO 181", "Tempe MUR 101"),
    ]

    walks = capacity_mod.walks_between(events, sett)

    assert len(walks) == 1
    assert walks[0].travel is True
    assert walks[0].ends_at == at(MONDAY, "10:30"), "the walk arrives as class starts"
    assert walks[0].minutes == 15


def test_two_classes_in_one_room_cost_nothing(sett) -> None:  # type: ignore[no-untyped-def]
    events = [
        klass(MONDAY, "09:00", "10:15", "CHM 113", "Tempe LSA 191"),
        klass(MONDAY, "10:30", "11:45", "CHM 113 Recitation", "LSA 191"),
    ]

    assert capacity_mod.walks_between(events, sett) == [], (
        "the same room reached the ledger spelled two ways and bought a walk"
    )


def test_a_walk_never_takes_more_than_the_gap(sett) -> None:  # type: ignore[no-untyped-def]
    """A ten-minute gap between two buildings is ten minutes of walking and no free
    time — which is the true reading of that gap, and the opposite of what the planner
    believed before this existed."""
    events = [
        klass(MONDAY, "09:00", "10:20", "CIS 236", "Tempe BA 396"),
        klass(MONDAY, "10:30", "11:45", "BIO 181", "Tempe MUR 101"),
    ]

    walks = capacity_mod.walks_between(events, sett)

    assert walks[0].minutes == 10
    assert walks[0].starts_at == at(MONDAY, "10:20")


def test_back_to_back_classes_get_no_walk_drawn_over_them(sett) -> None:  # type: ignore[no-untyped-def]
    """There is no minute to put it in, and a walk drawn over a class the owner is
    sitting in would be a plan asserting they are in two places."""
    events = [
        klass(MONDAY, "09:00", "10:30", "CIS 236", "Tempe BA 396"),
        klass(MONDAY, "10:30", "11:45", "BIO 181", "Tempe MUR 101"),
    ]

    assert capacity_mod.walks_between(events, sett) == []


def test_a_routine_has_no_room_and_so_no_walk(sett) -> None:  # type: ignore[no-untyped-def]
    events = [
        klass(MONDAY, "09:00", "10:15", "CIS 236", "Tempe BA 396"),
        routine(MONDAY, "12:30", "13:15", "Lunch"),
    ]

    assert capacity_mod.walks_between(events, sett) == []


def test_walking_is_subtracted_from_the_day(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """The point of the whole thing: the gap stops being plannable time."""
    events = [
        klass(MONDAY, "09:00", "10:15", "CIS 236", "Tempe BA 396"),
        klass(MONDAY, "10:30", "11:45", "BIO 181", "Tempe MUR 101"),
    ]
    walks = capacity_mod.walks_between(events, sett)

    cap = capacity_mod.compute(conn, sett, MONDAY, events=events + walks)

    assert cap.travel_minutes == 15


def test_zero_switches_walking_off(sett) -> None:  # type: ignore[no-untyped-def]
    events = [
        klass(MONDAY, "09:00", "10:15", "CIS 236", "Tempe BA 396"),
        klass(MONDAY, "10:30", "11:45", "BIO 181", "Tempe MUR 101"),
    ]

    assert capacity_mod.walks_between(events, sett.model_copy(update={"walk_minutes": 0})) == []


# ── what relief may take ──────────────────────────────────────────────────


def _evening(day: date) -> list[capacity_mod.FixedEvent]:
    return [
        routine(day, "07:30", "08:00", "Breakfast"),
        routine(day, "12:30", "13:15", "Lunch"),
        routine(day, "17:30", "18:30", "Gym"),
        routine(day, "18:35", "19:00", "Shower"),
        routine(day, "19:15", "20:00", "Dinner"),
        routine(day, "21:00", "23:00", "Relax"),
    ]


def test_relax_is_what_relief_takes_first(sett) -> None:  # type: ignore[no-untyped-def]
    kept, yields = capacity_mod.relieved(_evening(MONDAY), sett)

    assert [y.title for y in yields] == ["Relax"]
    assert all(y.removed for y in yields)
    assert "Relax" not in [e.title for e in kept]


def test_meals_and_shower_are_never_touched(sett) -> None:  # type: ignore[no-untyped-def]
    """They were not named, and a planner that skips dinner to fit a practice quiz is a
    planner that gets turned off in week one. This is the floor, and it is the test that
    matters most in this file."""
    kept, _ = capacity_mod.relieved(_evening(MONDAY), sett, compress=True)

    titles = [e.title for e in kept]
    for meal in ("Breakfast", "Lunch", "Dinner", "Shower"):
        assert meal in titles, f"relief took {meal}"
        original = next(e for e in _evening(MONDAY) if e.title == meal)
        assert next(e for e in kept if e.title == meal).minutes == original.minutes


def test_the_gym_is_shortened_and_never_deleted(sett) -> None:  # type: ignore[no-untyped-def]
    kept, yields = capacity_mod.relieved(_evening(MONDAY), sett, compress=True)

    gym = next(e for e in kept if e.title == "Gym")
    assert gym.minutes == 30, "the floor is half, not zero"
    assert any(y.title == "Gym" and not y.removed for y in yields)


def test_the_gym_is_left_alone_until_the_evening_was_not_enough(sett) -> None:  # type: ignore[no-untyped-def]
    """The owner's word was "or": remove relax and mcat study time, OR reduce time for
    other things. An escalation, not a list — and the first run of this cut an hour of
    exercise on a day that had already found the room without it."""
    kept, yields = capacity_mod.relieved(_evening(MONDAY), sett)

    assert next(e for e in kept if e.title == "Gym").minutes == 60
    assert "Gym" not in [y.title for y in yields]


def test_a_class_can_never_yield(sett) -> None:  # type: ignore[no-untyped-def]
    events = [*_evening(MONDAY), klass(MONDAY, "09:00", "10:15", "CIS 236", "BA 396")]

    kept, _ = capacity_mod.relieved(events, sett, compress=True)

    assert "CIS 236" in [e.title for e in kept]


def test_relief_can_be_switched_off_entirely(sett) -> None:  # type: ignore[no-untyped-def]
    off = sett.model_copy(update={"relief_window_end": ""})
    assert capacity_mod.relief_window_end(off, MONDAY) is None


def test_relief_only_ever_extends_a_day(sett) -> None:  # type: ignore[no-untyped-def]
    """A misconfigured value that would shorten the day is ignored, not obeyed."""
    early = sett.model_copy(update={"relief_window_end": "06:00"})
    from backglass.plan import timezones

    assert capacity_mod.relief_window_end(early, MONDAY) == timezones.window_on(
        early, MONDAY
    )[1]


# ── when relief may fire ──────────────────────────────────────────────────


def test_a_backlog_alone_never_costs_the_evening(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """The load-bearing safety property. The owner's board carries 255 hours against a
    day of two or three free ones, so *something* overflows every single day and always
    will. Under a plain "if anything overflowed" trigger, Relax would be deleted every
    evening for the rest of the semester."""
    for i in range(40):
        add_commitment(conn, sett, f"someday task {i}", minutes=90, due=None, n=i)

    proposal = planner.propose(conn, sett, MONDAY)

    assert proposal.overflow, "the fixture did not actually overflow, so this proves nothing"
    assert not proposal.relieved
    assert not any("gave up" in n for n in proposal.notes)


def test_work_due_today_that_will_not_fit_does_cost_the_evening(conn, sett) -> None:  # type: ignore[no-untyped-def]
    for i in range(12):
        add_commitment(conn, sett, f"due today {i}", minutes=90, due=MONDAY, n=i)

    proposal = planner.propose(conn, sett, MONDAY)

    assert proposal.relieved
    assert any("Relax gave up" in n for n in proposal.notes)


def test_a_day_that_fits_is_planned_exactly_as_it_was(conn, sett) -> None:  # type: ignore[no-untyped-def]
    add_commitment(conn, sett, "one small thing", minutes=30, due=MONDAY)

    proposal = planner.propose(conn, sett, MONDAY)

    assert not proposal.relieved
    assert "Relax" in [str(b["title"]) for b in proposal.blocks]


def test_relief_says_what_it_took(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """P18's rule, which relief needs more than any routine ever did: an evening that
    disappears without a sentence is one the owner discovers at nine o'clock."""
    for i in range(12):
        add_commitment(conn, sett, f"due today {i}", minutes=90, due=MONDAY, n=i)

    proposal = planner.propose(conn, sett, MONDAY)

    said = [n for n in proposal.notes if "gave up" in n or "was cut by" in n]
    assert said, "the evening went and the plan did not say so"
    assert all("so work due now could fit" in n for n in said)


def test_a_day_no_evening_can_save_keeps_the_cheaper_plan(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """Relief has to pay for itself. Three duplicate welcome surveys due today are
    unreachable at every level, and escalating on their account would cut the gym every
    day for the rest of the semester and place nothing extra."""
    add_commitment(conn, sett, "impossible today", minutes=6_000, due=MONDAY,
                   estimate_source="analyzed")

    proposal = planner.propose(conn, sett, MONDAY)

    gym = [b for b in proposal.blocks if str(b["title"]) == "Gym"]
    assert gym, "the gym left the day"
    assert int(gym[0]["minutes"]) == 60, "the gym was spent on work relief cannot place"
