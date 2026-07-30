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

_LEADING = re.compile(r"^(?:by|on|before|due|due\s+by|this|coming|end\s+of)\s+", re.IGNORECASE)
_IN_N = re.compile(r"^in\s+(\d+)\s+(day|days|week|weeks|month|months)$", re.IGNORECASE)
_N_FROM = re.compile(r"^(\d+)\s+(day|days|week|weeks)\s+from\s+now$", re.IGNORECASE)


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


def resolve_due(raw: str | None, *, occurred_at: str) -> Resolution:
    """Resolve a due date against the message timestamp.

    `occurred_at` is keyword-only and required. There is no overload that defaults it to
    the current time, and there must never be one.
    """
    if raw is None or not str(raw).strip():
        return Resolution(value=None)

    base = local_date_of(occurred_at)
    text = str(raw).strip()

    absolute = _try_absolute(text)
    if absolute is not None:
        return _guard(absolute, base, text, was_relative=False)

    relative = _try_relative(text, base)
    if relative is not None:
        return _guard(relative.isoformat(), base, text, was_relative=True)

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


def _try_relative(text: str, base: date) -> date | None:
    lowered = _LEADING.sub("", text.strip().lower()).strip().rstrip(".")
    lowered = re.sub(r"^next\s+", "next ", lowered)

    if lowered in {"today", "eod", "end of day", "cob", "close of business"}:
        return base
    if lowered == "tomorrow":
        return base + timedelta(days=1)
    if lowered == "yesterday":
        return base - timedelta(days=1)
    if lowered in {"week", "this week", "eow"}:
        # "end of week" — the Friday of the message's own week, or the coming Friday if
        # the message was sent at the weekend.
        return _next_weekday(base, 4, inclusive=True)
    if lowered in {"next week", "the week"}:
        return base + timedelta(days=7)
    if lowered in {"month", "next month", "end of month"}:
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


def _guard(value: str, base: date, original: str, *, was_relative: bool) -> Resolution:
    """Reject a due date that falls before the message that created it.

    A commitment cannot be due before it was made. When this fires, the usual cause is a
    date resolved against the wrong year, and a null due date that shows up in the review
    queue is a far better outcome than a confidently wrong one in the brief.
    """
    resolved = date.fromisoformat(value[:10])
    if resolved < base:
        return Resolution(
            value=None,
            note=(
                f"{original!r} resolved to {resolved.isoformat()}, before the message date "
                f"{base.isoformat()}; dropped"
            ),
            was_relative=was_relative,
        )
    return Resolution(value=value, was_relative=was_relative)
