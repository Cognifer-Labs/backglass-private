"""Relative date resolution. CLAUDE.md rule 4.

    "Relative dates resolve against the source item's timestamp, not the run time.
    'By Friday' in a three-week-old email is not this Friday."

This module has no access to the current time. There is no import of `datetime.now`,
`date.today`, or `time`, and `tests/test_dates.py` asserts that by reading this file's
source. That is deliberate: the bug rule 4 describes is not one you catch by testing
harder, it is one you make unrepresentable. Every function here takes the message
timestamp as a required argument.

The prompt asks the model to resolve dates itself and return ISO 8601. This module is
the check on that, plus the fallback for when it returns a bare weekday anyway.

Academic sources drift furthest from the prompt: syllabi say "assignments due March 3",
Canvas assignment text says "Jan 5th", professors write "submit by 5pm Friday". Those
shapes are parsed here — month names in both orders, with or without an ordinal suffix
or a year, and an optional clock time lifted out of the phrase. Numeric slash dates
(03/04/2027) are deliberately NOT parsed: the owner reads both US and Indian
conventions, so that string has two readings and this module never picks one.

Nothing here guesses. Every shape it cannot pin down exactly falls through to the same
drop-with-note path a commitment with no date takes. A wrong due date puts a fake
deadline in the day plan, which is strictly worse than no deadline at all.

Timezone handling: `occurred_at` carries the sender's UTC offset, preserved from the
`Date` header by connectors/gmail.py. The local date is read in that offset, so a message
sent at 19:00 in Phoenix (UTC-7) and one sent at 07:00 the next morning in Coimbatore
(UTC+5:30) resolve their weekdays in their own calendars, which is the Phoenix-to-
Coimbatore case docs/09 names in the Phase 4 exit criterion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

WEEKDAYS = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

# Lead-ins that carry no date information. Stripped repeatedly, because syllabus and
# professor prose stacks them: "due by Friday", "submitted by 5pm on March 3".
_PREFIX = re.compile(
    r"^(?:by|on|before|due(?:\s+by)?|no\s+later\s+than|nlt|this|coming|the"
    r"|submit(?:ted)?\s+by|turn\s+in\s+by)\s+"
)
# "end of the week", "end of this week" and "end of week" are one phrase, not three.
_END_OF = re.compile(r"^end\s+of\s+(?:the\s+|this\s+)?")
_IN_N = re.compile(r"^in\s+(\d+)\s+(day|days|week|weeks|month|months)$")
_N_FROM = re.compile(r"^(\d+)\s+(day|days|week|weeks)\s+from\s+now$")

# Longest first, so "sept" wins over "sep" and "march" over "mar".
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))
_ORDINAL = r"(?:st|nd|rd|th)?"
# "March 3", "Jan 5th", "Mar 3, 2027". Anchored: a month name buried in a sentence is
# not a date this module will guess at.
_MONTH_DAY = re.compile(
    rf"^(?P<month>{_MONTH_ALT})\.?\s+(?P<day>\d{{1,2}}){_ORDINAL}(?:,?\s+(?P<year>\d{{4}}))?$"
)
# "3 March", "3rd of March 2027".
_DAY_MONTH = re.compile(
    rf"^(?P<day>\d{{1,2}}){_ORDINAL}\s+(?:of\s+)?(?P<month>{_MONTH_ALT})\.?"
    rf"(?:,?\s+(?P<year>\d{{4}}))?$"
)

# A clock time anywhere in the phrase: "by 5pm Friday", "Friday at 5", "17:00 Friday",
# "March 3 at 11:59pm". The bare-integer case is only a time when "at"/"@" introduces it
# or a ":" or meridiem confirms it — otherwise the "3" in "March 3" would be read as
# three o'clock. That check is in _split_time, not the pattern.
_TIME = re.compile(
    r"""(?:^|\s)
        (?:(?P<at>at|@)\s*)?
        (?P<hour>\d{1,2})
        (?::(?P<minute>\d{2}))?
        \s*
        (?P<meridiem>a\.?m\.?|p\.?m\.?)?
        (?=$|\s|[,;.])
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class Resolution:
    #: ISO 8601 date or datetime, or None when nothing usable was stated.
    value: str | None
    #: Set when the input was rejected or adjusted. Ends up in the run report so a
    #: systematic date failure is visible rather than silently producing null due dates.
    note: str | None = None
    was_relative: bool = False


def local_date_of(occurred_at: str) -> date:
    """The calendar date at the sender's offset. The base for every resolution here."""
    return parse_occurred_at(occurred_at).date()


def parse_occurred_at(occurred_at: str) -> datetime:
    text = occurred_at.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed


def resolve_due(
    raw: str | None, *, occurred_at: str, allow_past: bool = False
) -> Resolution:
    """Resolve a due date against the message timestamp.

    `occurred_at` is keyword-only and required. There is no overload that defaults it to
    the current time, and there must never be one.

    `allow_past` turns off the forward guard below. A due date cannot precede the message
    that created it, so the default corrects an apparent slip by a year — but not every
    date a message names is a deadline. An engagement's `replaces_start_at` names where a
    plan ALREADY IS, and a message sent on the 18th moving "the 17th's dinner" means the
    17th, not the 17th of next year. Rolling that forward aimed the move at nothing and
    quietly filed a second plan.
    """
    if raw is None or not str(raw).strip():
        return Resolution(value=None)

    base = local_date_of(occurred_at)
    text = str(raw).strip()

    absolute = _try_absolute(text)
    if absolute is not None:
        if allow_past:
            return Resolution(value=absolute)
        return _guard(absolute, base, text, was_relative=False)

    # Everything below works on one lowercased, lead-in-stripped form of the phrase.
    phrase = _normalize(text)

    # A clock time is lifted out before the date is read, so "by 5pm Friday" and "Friday"
    # take the same weekday path. `time_note` is set when a time was stated but could not
    # be pinned down — the date still resolves, the time degrades to a note.
    remainder, clock, time_note = _split_time(phrase)

    named = _try_month_name(remainder, base)
    if named is not None:
        if allow_past:
            return Resolution(value=_with_time(named, clock), note=time_note)
        return _guard(_with_time(named, clock), base, text, was_relative=False, note=time_note)

    relative = _try_relative(remainder, base)
    if relative is not None:
        return _guard(
            _with_time(relative.isoformat(), clock),
            base,
            text,
            was_relative=True,
            note=time_note,
        )

    return Resolution(value=None, note=f"could not resolve {text!r} against {base.isoformat()}")


def _try_absolute(text: str) -> str | None:
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        try:
            return date.fromisoformat(candidate).isoformat()
        except ValueError:
            return None
    # A bare date is stored as a date; a datetime keeps its time and offset.
    if parsed.hour or parsed.minute or parsed.second or parsed.tzinfo:
        return parsed.replace(microsecond=0).isoformat()
    return parsed.date().isoformat()


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace, strip stacked lead-ins, canonicalise "end of X".

    Stripping is a loop rather than one pass because the lead-ins compose: "due by on
    March 3" is real prose from a syllabus. "end of the week" is rewritten to "end of
    week" here so that the phrase table below holds one entry per meaning.
    """
    lowered = " ".join(text.lower().split()).strip(" .,;")
    while True:
        stripped = _PREFIX.sub("", lowered).strip()
        if stripped == lowered:
            break
        lowered = stripped
    return _END_OF.sub("end of ", lowered)


def _split_time(phrase: str) -> tuple[str, tuple[int, int] | None, str | None]:
    """Lift a clock time out of a normalised phrase.

    Returns the phrase with the time removed, the (hour, minute) if one was stated
    unambiguously, and a note when a time was stated but could not be read as one hour.

    "Friday at 5" is the ambiguous case: 5am and 5pm are both ordinary readings and this
    module does not get to pick. The date still resolves — dropping the whole due date
    over an ambiguous hour would be worse — but the time is discarded and said so.
    """
    for match in _TIME.finditer(phrase):
        meridiem = match.group("meridiem")
        minute_text = match.group("minute")
        introduced = match.group("at") is not None
        if meridiem is None and minute_text is None and not introduced:
            # A bare integer with nothing marking it as a time — the "3" in "March 3".
            continue

        hour = int(match.group("hour"))
        minute = int(minute_text) if minute_text is not None else 0
        remainder = f"{phrase[: match.start()]} {phrase[match.end() :]}"
        remainder = _normalize(remainder)

        if meridiem is not None:
            if not 1 <= hour <= 12 or minute > 59:
                return remainder, None, f"unreadable clock time in {phrase!r}; time dropped"
            hour = hour % 12 + (12 if meridiem.startswith("p") else 0)
            return remainder, (hour, minute), None
        if hour > 23 or minute > 59:
            return remainder, None, f"unreadable clock time in {phrase!r}; time dropped"
        if minute_text is None and hour < 13:
            # "at 5" — am or pm, and guessing puts a fake deadline in the day plan.
            ambiguous = f"ambiguous clock time in {phrase!r}; date kept, time dropped"
            return remainder, None, ambiguous
        return remainder, (hour, minute), None
    return phrase, None, None


def _with_time(iso_date: str, clock: tuple[int, int] | None) -> str:
    """Attach a stated clock time to a resolved date.

    `commitment.due_at` is TEXT and already stores datetimes when the model returns one,
    and every reader truncates with `[:10]` before comparing days, so carrying the time
    costs nothing and no schema changes. No offset is attached: the prompt tells the model
    to keep what was written and never convert timezones, and a naive local time is what
    "5pm" means to the person who wrote it.
    """
    if clock is None:
        return iso_date
    return f"{iso_date}T{clock[0]:02d}:{clock[1]:02d}:00"


def _try_month_name(phrase: str, base: date) -> str | None:
    """"March 3", "3 March", "Jan 5th", "Mar 3, 2027" — the syllabus shapes.

    When no year is written, the answer is the first occurrence on or after the message
    date. A December email saying "January 5" means the January that is three weeks away,
    not the one ten months gone.
    """
    match = _MONTH_DAY.match(phrase) or _DAY_MONTH.match(phrase)
    if match is None:
        return None

    month = MONTHS[match.group("month")]
    day = int(match.group("day"))
    written_year = match.group("year")

    if written_year is not None:
        stated = _make_date(int(written_year), month, day)
        return stated.isoformat() if stated is not None else None

    # Five candidate years covers "February 29" from any starting year.
    for year in range(base.year, base.year + 5):
        candidate = _make_date(year, month, day)
        if candidate is not None and candidate >= base:
            return candidate.isoformat()
    return None


def _make_date(year: int, month: int, day: int) -> date | None:
    """A calendar date, or None when that day does not exist in that month."""
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _try_relative(phrase: str, base: date) -> date | None:
    lowered = phrase

    if lowered in {"today", "eod", "end of day", "cob", "close of business"}:
        return base
    if lowered == "tomorrow":
        return base + timedelta(days=1)
    if lowered == "yesterday":
        return base - timedelta(days=1)
    # "this week" arrives here as "week": _normalize strips the lead-in.
    if lowered in {"week", "eow", "end of week"}:
        # The Friday of the message's own week, or the coming Friday if the message was
        # sent at the weekend.
        return _next_weekday(base, 4, inclusive=True)
    if lowered == "next week":
        return base + timedelta(days=7)
    if lowered in {"month", "next month", "end of month", "eom"}:
        return _end_of_month(base)

    match = _IN_N.match(lowered) or _N_FROM.match(lowered)
    if match:
        count, unit = int(match.group(1)), match.group(2).rstrip("s")
        if unit == "day":
            return base + timedelta(days=count)
        if unit == "week":
            return base + timedelta(weeks=count)
        return _add_months(base, count)

    is_next = lowered.startswith("next ")
    name = lowered.removeprefix("next ").strip()
    if name in WEEKDAYS:
        # "By Friday" in a message sent on Friday 2026-07-10 means 2026-07-17, per the
        # worked example in specs/extraction-prompts/extract-commitments.md. Strictly
        # after, never the same day.
        target = _next_weekday(base, WEEKDAYS[name], inclusive=False)
        return target + timedelta(days=7) if is_next and (target - base).days < 7 else target
    return None


def _next_weekday(base: date, weekday: int, *, inclusive: bool) -> date:
    ahead = (weekday - base.weekday()) % 7
    if ahead == 0 and not inclusive:
        ahead = 7
    return base + timedelta(days=ahead)


def _end_of_month(base: date) -> date:
    first_next = _add_months(base.replace(day=1), 1)
    return first_next - timedelta(days=1)


def _add_months(base: date, count: int) -> date:
    month_index = base.month - 1 + count
    year = base.year + month_index // 12
    month = month_index % 12 + 1
    last = [31, 29 if _leap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(base.day, last))


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _guard(
    value: str, base: date, original: str, *, was_relative: bool, note: str | None = None
) -> Resolution:
    """Reject a due date that falls before the message that created it.

    A commitment cannot be due before it was made. When this fires, the usual cause is a
    date resolved against the wrong year, and a null due date that shows up in the review
    queue is a far better outcome than a confidently wrong one in the brief.

    An overdue commitment is one due before *today*, which this function never sees and
    must never touch. It only sees dates due before the message that created them, and
    those are already being dropped outright — so trying a year correction first cannot
    turn a legitimately overdue commitment into a future one. It can only turn a dropped
    date into a kept one.

    The correction is +1 year, taken only when it lands on or after the message date.
    That arithmetic bounds itself: a date before `base` that is still before `base` after
    a year has been added is not a rollover slip, and it falls through to the drop. No
    separate "within 11 months" window is needed, and imposing one would break the exact
    case this exists for — a January message citing "2026-01-08" when it meant 2027.

    Relative phrases are never corrected. "Yesterday" resolves to the past because it
    means the past, and adding a year to it would invent a deadline nobody wrote.
    """
    resolved = date.fromisoformat(value[:10])
    if resolved < base:
        corrected = _make_date(resolved.year + 1, resolved.month, resolved.day)
        if not was_relative and corrected is not None and corrected >= base:
            return Resolution(
                value=corrected.isoformat() + value[10:],
                note=_join_notes(
                    note,
                    f"{original!r} resolved to {resolved.isoformat()}, before the message date "
                    f"{base.isoformat()}; read as {corrected.isoformat()}",
                ),
                was_relative=was_relative,
            )
        return Resolution(
            value=None,
            note=_join_notes(
                note,
                f"{original!r} resolved to {resolved.isoformat()}, before the message date "
                f"{base.isoformat()}; dropped",
            ),
            was_relative=was_relative,
        )
    return Resolution(value=value, note=note, was_relative=was_relative)


def _join_notes(*notes: str | None) -> str | None:
    stated = [note for note in notes if note]
    return "; ".join(stated) if stated else None
