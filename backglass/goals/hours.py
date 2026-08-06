"""What was logged, month by month.

The totals bars answer *how far*. They cannot answer *whether logging is still
happening*, and on a four-year accumulator that is the question: a research total at
60/200 renders identically whether the last entry was yesterday or last spring. This
module buckets the same checkpoints the bars sum into months so the page can say it.

**Bucketing rule.** A checkpoint is filed under the month its own timestamp names, in
the offset that timestamp carries — never converted to another zone. A manual log is
stamped by `local_noon_iso` in the owner's local frame, so the written date is already
the day the owner meant; converting it would move a Phoenix evening into the next month
in UTC and file an hour under a month nobody worked it in.

That rule is also what keeps this module clear of the mixed-offset trap `tasks/lessons.md`
describes four times: nothing here compares, sorts or aggregates two timestamps. Each
one is parsed alone and reduced to `(year, month)`, and every comparison after that is
between integer pairs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

#: How many months the strip shows. A pre-med hour log is read as "the last year",
#: and twelve columns still carry a readable month label at dashboard width.
WINDOW_MONTHS = 12

#: Shortest bar a non-zero month may draw, as a percentage of the axis. One hour
#: against a 46-hour peak is 2% of the plot — a hairline indistinguishable from the
#: empty months either side of it. A month with something in it must not read as a
#: month with nothing in it.
MIN_BAR_PCT = 6.0

_MONTH_ABBR = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)  # fmt: skip


@dataclass(frozen=True)
class Month:
    """One column: a calendar month and what was logged in it."""

    year: int
    month: int
    hours: int
    pct: float
    is_current: bool

    @property
    def label(self) -> str:
        return _MONTH_ABBR[self.month - 1]

    @property
    def full(self) -> str:
        return f"{_MONTH_ABBR[self.month - 1]} {self.year}"


@dataclass(frozen=True)
class Monthly:
    """The whole strip, plus the two figures that make it legible.

    `axis_max` is what 100% of the plot height means. It is the peak month *or* the
    required pace, whichever is larger — a pace line above every bar has to stay inside
    the plot or it cannot be drawn at all, and a pace that far above the log is exactly
    the case worth seeing.
    """

    months: list[Month]
    logged: int
    peak: int
    axis_max: int
    pace: int | None
    remaining: int

    @property
    def has_data(self) -> bool:
        return self.logged > 0

    @property
    def pace_pct(self) -> float:
        """Where the reference line sits, as a percentage from the plot's floor."""
        if not self.pace or self.axis_max <= 0:
            return 0.0
        return min(100.0, 100.0 * self.pace / self.axis_max)


def month_of(stamp: str) -> tuple[int, int] | None:
    """The `(year, month)` a timestamp names in its own offset, or None if unparseable.

    Unparseable is a skipped column, never a raised exception: this feeds a page, and
    rule 5's "degrade, never block" has the row as its unit. One malformed stamp in a
    four-year ledger must not take the chart, and the number it carries is still in the
    totals bar above — which is summed by SQLite and never parsed at all.
    """
    try:
        parsed = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return (parsed.year, parsed.month)


def window(today: date, months: int = WINDOW_MONTHS) -> list[tuple[int, int]]:
    """The `months` calendar months ending with `today`'s, oldest first."""
    keys: list[tuple[int, int]] = []
    year, month = today.year, today.month
    for _ in range(max(1, months)):
        keys.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(keys))


def months_until(today: date, target: str | None) -> int | None:
    """Whole calendar months from `today` to `target`, at least 1, or None.

    Month arithmetic rather than days: the pace figure is quoted per month, and
    "38 months left" divides a remainder into something a person can act on where
    "1,163 days" does not. The floor of 1 means a target inside this month asks for
    the whole remainder now, which is true.
    """
    if not target:
        return None
    try:
        end = date.fromisoformat(str(target)[:10])
    except ValueError:
        return None
    if end <= today:
        return None
    return max(1, (end.year - today.year) * 12 + (end.month - today.month))


def monthly(
    totals: list[dict[str, Any]],
    *,
    today: date,
    target_date: str | None = None,
    months: int = WINDOW_MONTHS,
) -> Monthly:
    """Every total's logged entries, bucketed into the trailing `months` months.

    `totals` is what `roadmaps.totals_for` returns: each row carries `entries`, the
    full checkpoint stream behind its bar. Summing the same rows the bars sum is the
    point — the chart cannot disagree with the number above it, because there is only
    one source (G3/G10: everything here is computed on read).
    """
    keys = window(today, months)
    buckets = dict.fromkeys(keys, 0)
    remaining = 0
    for total in totals:
        done = int(total.get("done") or 0)
        goal = int(total.get("total_count") or 0)
        remaining += max(0, goal - done)
        for entry in total.get("entries") or []:
            key = month_of(str(entry["occurred_at"]))
            if key in buckets:
                buckets[key] += int(entry["delta"] or 0)

    logged = sum(buckets.values())
    peak = max(buckets.values(), default=0)
    left = months_until(today, target_date)
    pace = math.ceil(remaining / left) if (left and remaining > 0) else None
    axis_max = max(peak, pace or 0)

    current = (today.year, today.month)
    return Monthly(
        months=[
            Month(
                year=y,
                month=m,
                hours=buckets[(y, m)],
                pct=_pct(buckets[(y, m)], axis_max),
                is_current=(y, m) == current,
            )
            for (y, m) in keys
        ],
        logged=logged,
        peak=peak,
        axis_max=axis_max,
        pace=pace,
        remaining=remaining,
    )


def _pct(hours: int, axis_max: int) -> float:
    if hours <= 0 or axis_max <= 0:
        return 0.0
    return max(MIN_BAR_PCT, min(100.0, 100.0 * hours / axis_max))
