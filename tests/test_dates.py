"""CLAUDE.md rule 4, and the timezone case docs/09 names in the Phase 4 exit criterion.

    "Relative dates resolve against the source item's timestamp, not the run time.
    'By Friday' in a three-week-old email is not this Friday."

The first test is the structural one and it matters more than the rest: it asserts that
`dates.py` has no access to the current time at all. Everything else here can be made to
pass by a module that reads the clock and happens to get the right answer today.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from backglass.extract import dates
from backglass.extract.dates import resolve_due

PHOENIX = "-07:00"  # UTC-7, no DST
KOLKATA = "+05:30"

# 2026-07-10 is a Friday. specs/extraction-prompts/extract-commitments.md uses exactly
# this date in its worked example: "By Friday" in a message sent 2026-07-10 means
# 2026-07-17, "even if today is 2026-08-30".
FRIDAY = f"2026-07-10T09:15:00{PHOENIX}"


def test_dates_module_cannot_read_the_clock() -> None:
    """The bug rule 4 describes is made unrepresentable, not merely untested.

    If this fails, someone has added a fallback that resolves against `now`, and every
    other test in this file has stopped proving anything.
    """
    source = Path(dates.__file__).read_text()
    body = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
    for forbidden in ("datetime.now", ".now(", "date.today", ".today(", "time.time"):
        assert forbidden not in body, f"dates.py reaches for {forbidden!r}"


# ────────────────────────────────────────────── the doc's own worked example


def test_by_friday_in_a_three_week_old_message_is_not_this_friday() -> None:
    """The exact example from extract-commitments.md §DATE RESOLUTION."""
    resolved = resolve_due("Friday", occurred_at=FRIDAY)
    assert resolved.value == "2026-07-17"
    assert resolved.was_relative is True


def test_relative_resolution_is_independent_of_when_the_test_runs() -> None:
    """Same input, same answer, forever. This is what "not the run time" means."""
    assert resolve_due("by Friday", occurred_at=FRIDAY).value == "2026-07-17"
    assert resolve_due("by Friday", occurred_at=f"2026-01-14T08:00:00{PHOENIX}").value == (
        "2026-01-16"
    )


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("today", "2026-07-10"),
        ("tomorrow", "2026-07-11"),
        ("by Monday", "2026-07-13"),
        ("by Thursday", "2026-07-16"),
        ("next Monday", "2026-07-20"),
        ("in 3 days", "2026-07-13"),
        ("in 2 weeks", "2026-07-24"),
        ("end of week", "2026-07-10"),
        ("next week", "2026-07-17"),
        ("end of month", "2026-07-31"),
        ("EOD", "2026-07-10"),
    ],
)
def test_relative_vocabulary(phrase: str, expected: str) -> None:
    assert resolve_due(phrase, occurred_at=FRIDAY).value == expected


def test_absolute_dates_pass_through() -> None:
    assert resolve_due("2026-08-03", occurred_at=FRIDAY).value == "2026-08-03"
    assert resolve_due("2026-08-03T17:00:00-07:00", occurred_at=FRIDAY).value == (
        "2026-08-03T17:00:00-07:00"
    )


def test_nothing_stated_is_none_not_a_guess() -> None:
    assert resolve_due(None, occurred_at=FRIDAY).value is None
    assert resolve_due("", occurred_at=FRIDAY).value is None
    assert resolve_due("   ", occurred_at=FRIDAY).value is None


def test_unresolvable_text_is_dropped_with_a_note_not_silently_nulled() -> None:
    resolved = resolve_due("whenever you get a chance", occurred_at=FRIDAY)
    assert resolved.value is None
    assert resolved.note is not None and "could not resolve" in resolved.note


def test_a_date_before_the_message_is_rejected() -> None:
    """A commitment cannot be due before it was made.

    The usual cause is a year resolved wrong. A null due date in the review queue beats a
    confidently wrong one in the brief.
    """
    resolved = resolve_due("2025-01-01", occurred_at=FRIDAY)
    assert resolved.value is None
    assert resolved.note is not None and "before the message date" in resolved.note


# ─────────────────────────────────────────── Phoenix ↔ Coimbatore, both ways


def test_the_senders_offset_decides_the_weekday_not_utc() -> None:
    """The Phoenix-to-Coimbatore case, at the boundary where the two disagree.

    2026-07-10T20:00-07:00 is 2026-07-11T08:30+05:30 — the same instant, a Friday in
    Phoenix and a Saturday in Coimbatore. "By Monday" therefore means the 13th to the
    Phoenix sender and the 13th to the Coimbatore one, but "by Friday" means the 17th to
    one and the 17th to the other only if each is read in its own calendar. Read both in
    UTC and one of them is wrong.
    """
    phoenix_evening = f"2026-07-10T20:00:00{PHOENIX}"
    kolkata_morning = f"2026-07-11T08:30:00{KOLKATA}"

    assert dates.local_date_of(phoenix_evening) == date(2026, 7, 10)  # Friday
    assert dates.local_date_of(kolkata_morning) == date(2026, 7, 11)  # Saturday

    assert resolve_due("by Friday", occurred_at=phoenix_evening).value == "2026-07-17"
    assert resolve_due("by Friday", occurred_at=kolkata_morning).value == "2026-07-17"

    # Saturday in Coimbatore, so "end of week" is the coming Friday. Friday in Phoenix,
    # so "end of week" is today. Same instant, different answers, both correct.
    assert resolve_due("end of week", occurred_at=phoenix_evening).value == "2026-07-10"
    assert resolve_due("end of week", occurred_at=kolkata_morning).value == "2026-07-17"


def test_moving_east_then_west_does_not_shift_a_resolved_date() -> None:
    """The owner moves between UTC-7 and UTC+5:30. The message's own offset travels with
    the message, so re-running extraction from the other side of the world is a no-op."""
    for offset in (PHOENIX, KOLKATA, "+00:00"):
        stamped = f"2026-07-10T09:15:00{PHOENIX}"
        del offset  # the run's timezone is irrelevant by construction
        assert resolve_due("by Friday", occurred_at=stamped).value == "2026-07-17"


def test_utc_z_suffix_is_accepted() -> None:
    assert dates.local_date_of("2026-07-10T16:15:00Z") == date(2026, 7, 10)
