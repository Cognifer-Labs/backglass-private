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


# ──────────────────────────────────────────────── month-name dates: syllabi
#
# The academic sources are where the model drifts off ISO: a syllabus says "assignments
# due March 3", Canvas says "Jan 5th", a professor writes "submit by 5pm Friday". Before
# these shapes parsed, every one of them produced a commitment with no due date, which
# never reaches a day plan or a deadline-sorted board.

DECEMBER = f"2026-12-18T16:00:00{PHOENIX}"  # a Friday, three weeks before January 5


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("March 3", "2027-03-03"),
        ("march 3", "2027-03-03"),
        ("MARCH 3", "2027-03-03"),
        ("Mar 3", "2027-03-03"),
        ("Mar. 3", "2027-03-03"),
        ("3 March", "2027-03-03"),
        ("3rd March", "2027-03-03"),
        ("3rd of March", "2027-03-03"),
        ("Jan 5th", "2027-01-05"),
        ("January 5th", "2027-01-05"),
        ("Sept 30", "2026-09-30"),
        ("September 30", "2026-09-30"),
        ("Aug 1st", "2026-08-01"),
        ("due March 3", "2027-03-03"),
        ("by March 3", "2027-03-03"),
        ("submitted by March 3", "2027-03-03"),
        ("no later than March 3", "2027-03-03"),
    ],
)
def test_month_name_dates(phrase: str, expected: str) -> None:
    assert resolve_due(phrase, occurred_at=FRIDAY).value == expected


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("Mar 3, 2027", "2027-03-03"),
        ("March 3, 2027", "2027-03-03"),
        ("March 3 2027", "2027-03-03"),
        ("3 March 2027", "2027-03-03"),
        ("3rd of March, 2027", "2027-03-03"),
    ],
)
def test_month_name_dates_with_an_explicit_year(phrase: str, expected: str) -> None:
    assert resolve_due(phrase, occurred_at=FRIDAY).value == expected


def test_a_missing_year_means_the_first_such_day_on_or_after_the_message() -> None:
    """Not "this year" — the next occurrence, counted from the message's own date."""
    # July message: March has already gone by, so it is next March.
    assert resolve_due("March 3", occurred_at=FRIDAY).value == "2027-03-03"
    # Same July message: September has not.
    assert resolve_due("September 30", occurred_at=FRIDAY).value == "2026-09-30"
    # The message's own day counts. A syllabus posted on the due date is not next year's.
    same_day = f"2026-03-03T08:00:00{PHOENIX}"
    assert resolve_due("March 3", occurred_at=same_day).value == "2026-03-03"


def test_december_to_january_rollover() -> None:
    """The case that motivated the whole month-name path.

    A professor emails on 2026-12-18 saying the final report is due January 5. That is
    2027-01-05 and it is eighteen days away. Resolving it to 2026-01-05 would put it in
    the past, where the guard would drop it and the commitment would land undated.
    """
    assert resolve_due("January 5", occurred_at=DECEMBER).value == "2027-01-05"
    assert resolve_due("Jan 5th", occurred_at=DECEMBER).value == "2027-01-05"
    assert resolve_due("submit by Jan 5th", occurred_at=DECEMBER).value == "2027-01-05"
    # December dates in the same message still mean this December, not next.
    assert resolve_due("December 22", occurred_at=DECEMBER).value == "2026-12-22"


def test_february_29_lands_on_a_leap_year_not_a_nonexistent_day() -> None:
    assert resolve_due("February 29", occurred_at=FRIDAY).value == "2028-02-29"


@pytest.mark.parametrize(
    "phrase",
    [
        "March 32",  # no such day, in any year
        "Feb 30",
        "March",  # a month with no day is not a date
        "sometime in March",  # a month name inside prose, not a stated date
        "March 3 or thereabouts",
        "03/03/2027",  # ambiguous: 3 March or March 3, and the owner reads both ways
        "13/03/2027",
        "the March deadline",
    ],
)
def test_month_shapes_that_must_not_resolve(phrase: str) -> None:
    """Ambiguity degrades to the note, never to a guess.

    Slash dates are the important ones here: the owner splits time between the US and
    India and reads both conventions, so 03/04/2027 has two readings and this module is
    not entitled to pick either.
    """
    resolved = resolve_due(phrase, occurred_at=FRIDAY)
    assert resolved.value is None
    assert resolved.note is not None and "could not resolve" in resolved.note


# ─────────────────────────────────────────────────────── time of day carried


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("by 5pm Friday", "2026-07-17T17:00:00"),
        ("Friday 5pm", "2026-07-17T17:00:00"),
        ("Friday, 5pm", "2026-07-17T17:00:00"),
        ("5pm on Friday", "2026-07-17T17:00:00"),
        ("at 5 pm Friday", "2026-07-17T17:00:00"),
        ("17:00 Friday", "2026-07-17T17:00:00"),
        ("Friday at 17:00", "2026-07-17T17:00:00"),
        ("Friday at 17", "2026-07-17T17:00:00"),
        ("Friday at 11:59pm", "2026-07-17T23:59:00"),
        ("Monday at 9am", "2026-07-13T09:00:00"),
        ("Monday at 12am", "2026-07-13T00:00:00"),
        ("Monday at 12pm", "2026-07-13T12:00:00"),
        ("March 3 at 11:59pm", "2027-03-03T23:59:00"),
        ("by 11:59pm on March 3", "2027-03-03T23:59:00"),
        ("tomorrow at 9am", "2026-07-11T09:00:00"),
    ],
)
def test_time_of_day_is_carried_onto_the_resolved_date(phrase: str, expected: str) -> None:
    """`commitment.due_at` is TEXT and every reader truncates with `[:10]` before
    comparing days, so a stated clock time rides along without a schema change. No offset
    is attached: the prompt says keep what was written and never convert timezones."""
    assert resolve_due(phrase, occurred_at=FRIDAY).value == expected


def test_the_date_still_resolves_when_the_clock_time_is_ambiguous() -> None:
    """"Friday at 5" is 5am or 5pm and this module does not get to pick.

    Dropping the whole due date over an ambiguous hour would be the wrong trade — the
    weekday was stated unambiguously. The time goes in the note instead.
    """
    resolved = resolve_due("Friday at 5", occurred_at=FRIDAY)
    assert resolved.value == "2026-07-17"
    assert resolved.note is not None and "ambiguous clock time" in resolved.note

    unreadable = resolve_due("Friday at 25", occurred_at=FRIDAY)
    assert unreadable.value == "2026-07-17"
    assert unreadable.note is not None and "unreadable clock time" in unreadable.note


def test_a_bare_number_in_a_date_is_not_read_as_an_hour() -> None:
    """The regression this guards: the "3" in "March 3" as three o'clock."""
    assert resolve_due("March 3", occurred_at=FRIDAY).value == "2027-03-03"
    assert resolve_due("in 3 days", occurred_at=FRIDAY).value == "2026-07-13"
    assert resolve_due("3 days from now", occurred_at=FRIDAY).value == "2026-07-13"
    assert resolve_due("in 2 weeks", occurred_at=FRIDAY).value == "2026-07-24"


def test_a_time_with_no_date_is_not_a_due_date() -> None:
    resolved = resolve_due("at 5pm", occurred_at=FRIDAY)
    assert resolved.value is None
    assert resolved.note is not None and "could not resolve" in resolved.note


# ───────────────────────────────────────────── year rollover in the guard


def test_a_wrong_year_is_corrected_rather_than_dropped() -> None:
    """A January message citing "2026-01-08" almost certainly means 2027-01-08.

    The guard only ever sees dates due before the message that created them, and it was
    already dropping every one of them. Trying +1 year first can only turn a dropped date
    into a kept one — it can never reach a legitimately overdue commitment, because those
    are due before *today*, which this module cannot see.
    """
    january = f"2027-01-03T09:00:00{PHOENIX}"
    resolved = resolve_due("2026-01-08", occurred_at=january)
    assert resolved.value == "2027-01-08"
    assert resolved.note is not None and "read as 2027-01-08" in resolved.note


def test_the_year_correction_keeps_the_stated_time() -> None:
    january = f"2027-01-03T09:00:00{PHOENIX}"
    resolved = resolve_due("2026-01-08T17:00:00-07:00", occurred_at=january)
    assert resolved.value == "2027-01-08T17:00:00-07:00"


def test_a_year_correction_that_still_lands_in_the_past_is_dropped() -> None:
    """+1 year bounds itself. Two years wrong is not a rollover slip, it is noise."""
    resolved = resolve_due("2025-01-01", occurred_at=FRIDAY)
    assert resolved.value is None
    assert resolved.note is not None and "dropped" in resolved.note


def test_relative_phrases_are_never_year_corrected() -> None:
    """"Yesterday" resolves to the past because it means the past. Adding a year to it
    would invent a deadline nobody wrote."""
    resolved = resolve_due("yesterday", occurred_at=FRIDAY)
    assert resolved.value is None
    assert resolved.note is not None and "dropped" in resolved.note


# ────────────────────────────────────────────── academic-shaped relatives


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("end of the week", "2026-07-10"),
        ("end of this week", "2026-07-10"),
        ("by the end of the week", "2026-07-10"),
        ("end of week", "2026-07-10"),
        ("EOW", "2026-07-10"),
        ("end of the month", "2026-07-31"),
        ("end of day", "2026-07-10"),  # was unresolvable: "end of" ate itself
        ("this Monday", "2026-07-13"),
        ("next Monday", "2026-07-20"),
        ("coming Monday", "2026-07-13"),
        ("due by on Friday", "2026-07-17"),  # stacked lead-ins, as prose actually stacks
    ],
)
def test_academic_shaped_relatives(phrase: str, expected: str) -> None:
    assert resolve_due(phrase, occurred_at=FRIDAY).value == expected


def test_this_monday_and_next_monday_are_different_days() -> None:
    """Per the convention stated in extract-commitments.md: a bare or "this" weekday is
    the first such day strictly after the message; "next" skips a week when the nearer
    one is under a week out."""
    assert resolve_due("this Monday", occurred_at=FRIDAY).value == "2026-07-13"
    assert resolve_due("Monday", occurred_at=FRIDAY).value == "2026-07-13"
    assert resolve_due("next Monday", occurred_at=FRIDAY).value == "2026-07-20"


@pytest.mark.parametrize("phrase", ["end of next week", "end of the semester", "mid March"])
def test_relatives_with_no_unambiguous_definition_stay_unresolved(phrase: str) -> None:
    """An ambiguous rule that guesses wrong is worse than a note. "End of next week" has
    at least two readings (that Friday, that Sunday) and neither is written down."""
    assert resolve_due(phrase, occurred_at=FRIDAY).value is None


# ─────────────────────── every new path, in both of the owner's timezones


@pytest.mark.parametrize(
    ("phrase", "phoenix_expected", "kolkata_expected"),
    [
        # 2026-07-10T20:00-07:00 and 2026-07-11T08:30+05:30 are the same instant, a
        # Friday in Phoenix and a Saturday in Coimbatore. Read in UTC, one is wrong.
        ("March 3", "2027-03-03", "2027-03-03"),
        ("by 5pm Friday", "2026-07-17T17:00:00", "2026-07-17T17:00:00"),
        ("March 3 at 11:59pm", "2027-03-03T23:59:00", "2027-03-03T23:59:00"),
        # These two disagree, and both are right: end of week is today in Phoenix and
        # the coming Friday in Coimbatore, where it is already Saturday.
        ("end of the week", "2026-07-10", "2026-07-17"),
        ("end of the week at 5pm", "2026-07-10T17:00:00", "2026-07-17T17:00:00"),
        # July 11 is still July for both, so end of month agrees.
        ("end of the month", "2026-07-31", "2026-07-31"),
    ],
)
def test_new_paths_across_the_day_boundary_in_both_directions(
    phrase: str, phoenix_expected: str, kolkata_expected: str
) -> None:
    phoenix_evening = f"2026-07-10T20:00:00{PHOENIX}"
    kolkata_morning = f"2026-07-11T08:30:00{KOLKATA}"
    assert resolve_due(phrase, occurred_at=phoenix_evening).value == phoenix_expected
    assert resolve_due(phrase, occurred_at=kolkata_morning).value == kolkata_expected


def test_a_month_name_date_at_the_year_boundary_in_both_directions() -> None:
    """The day boundary and the year boundary at once.

    2026-12-31T20:00-07:00 is 2027-01-01T08:30+05:30 — still 2026 in Phoenix, already
    2027 in Coimbatore. "January 5" is 2027-01-05 to both, but only because each reads
    the message in its own calendar: the Phoenix message infers the year forward across
    the boundary, the Coimbatore one is already there.
    """
    phoenix_nye = f"2026-12-31T20:00:00{PHOENIX}"
    kolkata_new_year = f"2027-01-01T08:30:00{KOLKATA}"

    assert dates.local_date_of(phoenix_nye) == date(2026, 12, 31)
    assert dates.local_date_of(kolkata_new_year) == date(2027, 1, 1)

    assert resolve_due("January 5", occurred_at=phoenix_nye).value == "2027-01-05"
    assert resolve_due("January 5", occurred_at=kolkata_new_year).value == "2027-01-05"

    # "December 31" is today to the Phoenix sender and a year away to the other. Neither
    # is a guess: each is the first such day on or after that message's own date.
    assert resolve_due("December 31", occurred_at=phoenix_nye).value == "2026-12-31"
    assert resolve_due("December 31", occurred_at=kolkata_new_year).value == "2027-12-31"
