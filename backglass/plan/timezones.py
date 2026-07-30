"""The active timezone, and the two-zone display rule. docs/04 §1.7.

The owner moves between America/Phoenix (UTC-7, no DST) and Asia/Kolkata (UTC+5:30).

  P13  All timestamps stored UTC. The working window, brief delivery and day boundaries
       follow the *active* timezone, not a fixed offset.
  P14  The active timezone comes from an explicit setting with an optional date range,
       "not from IP geolocation, which is wrong exactly when travelling".
  P15  On a day where the active timezone changes, the brief leads with the change.
  P16  Meetings scheduled in the other zone display both local and counterpart time.

P14 deserves its emphasis. Geolocation is right on the 360 days you are at home and wrong
on the five that matter, and those five are exactly when a missed meeting is expensive.
An explicit range is a sentence in a config file and is never wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from backglass.config import Settings

#: `YYYY-MM-DD..YYYY-MM-DD:Area/Zone`, end inclusive; the end date may be omitted for an
#: open-ended stay. A bare `YYYY-MM-DD:Area/Zone` means "from this date onward".
_RANGE = re.compile(
    r"^(?P<start>\d{4}-\d{2}-\d{2})(?:\.\.(?P<end>\d{4}-\d{2}-\d{2}))?:(?P<zone>[\w/+\-]+)$"
)


class TimezoneError(ValueError):
    pass


@dataclass(frozen=True)
class Stay:
    start: date
    end: date | None
    zone: str

    def covers(self, day: date) -> bool:
        return self.start <= day and (self.end is None or day <= self.end)


def parse_ranges(entries: list[str]) -> list[Stay]:
    stays: list[Stay] = []
    for raw in entries:
        match = _RANGE.match(raw.strip())
        if not match:
            raise TimezoneError(
                f"bad TZ_RANGES entry {raw!r}; expected YYYY-MM-DD[..YYYY-MM-DD]:Area/Zone"
            )
        end = match.group("end")
        stays.append(
            Stay(
                start=date.fromisoformat(match.group("start")),
                end=date.fromisoformat(end) if end else None,
                zone=match.group("zone"),
            )
        )
    return stays


def active_tz(settings: Settings, day: date) -> str:
    """The zone in force on `day`. Later entries win on overlap.

    Later-wins rather than first-wins because the natural way to record a trip is to
    append it, and a trip appended after an open-ended "I live in Phoenix" entry must
    override it rather than be shadowed by it.
    """
    zone = settings.default_tz
    for stay in parse_ranges(settings.tz_ranges):
        if stay.covers(day):
            zone = stay.zone
    return zone


def changed_on(settings: Settings, day: date) -> tuple[str, str] | None:
    """P15. `(yesterday, today)` when the active zone changes on `day`, else None."""
    from datetime import timedelta

    previous = active_tz(settings, day - timedelta(days=1))
    current = active_tz(settings, day)
    return None if previous == current else (previous, current)


def parse_window(window: str) -> tuple[time, time]:
    """`09:00-18:00` into two times."""
    try:
        start, _, end = window.partition("-")
        return time.fromisoformat(start.strip()), time.fromisoformat(end.strip())
    except ValueError as exc:
        raise TimezoneError(f"bad window {window!r}; expected HH:MM-HH:MM") from exc


def window_on(
    settings: Settings, day: date, window: str | None = None
) -> tuple[datetime, datetime]:
    """The working window as two aware datetimes in the day's active zone.

    P13: the window follows the active timezone. Flying to Coimbatore does not move the
    owner's working day to 21:30; it moves the underlying UTC instants.
    """
    zone = ZoneInfo(active_tz(settings, day))
    start, end = parse_window(window or settings.working_window)
    return (
        datetime.combine(day, start, tzinfo=zone),
        datetime.combine(day, end, tzinfo=zone),
    )


def is_working_day(settings: Settings, day: date) -> bool:
    names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    return names[day.weekday()] in settings.working_days


def both_zones(moment: datetime, primary: str, counterpart: str) -> str:
    """P16. "A 09:00 Phoenix call is 21:30 in Coimbatore, and getting this wrong once
    costs a meeting."

    Rendered as one string rather than two fields because the whole point is that the two
    are read together.
    """
    here = moment.astimezone(ZoneInfo(primary))
    there = moment.astimezone(ZoneInfo(counterpart))
    if here.utcoffset() == there.utcoffset():
        return here.strftime("%H:%M")
    day_note = ""
    if there.date() > here.date():
        day_note = " next day"
    elif there.date() < here.date():
        day_note = " prev day"
    return (
        f"{here.strftime('%H:%M')} {_short(primary)} / "
        f"{there.strftime('%H:%M')}{day_note} {_short(counterpart)}"
    )


def _short(zone: str) -> str:
    return zone.rsplit("/", 1)[-1].replace("_", " ")
