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
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
        # The regex checks the *shape*; `fromisoformat` checks the *value*, and it
        # raises a plain ValueError. Two kinds of validation raising two kinds of
        # exception is how `2026-02-30` — a transposed leap day, a swapped month and
        # day — walked past every `except TimezoneError` in this module and tracebacked
        # out of `backglass log`, `plan`, `doctor` and three dashboard pages. Everything
        # this function rejects now leaves by the same door.
        try:
            start_on = date.fromisoformat(match.group("start"))
            end_on = date.fromisoformat(end) if end else None
        except ValueError as exc:
            raise TimezoneError(f"bad date in TZ_RANGES entry {raw!r}: {exc}") from exc
        stays.append(Stay(start=start_on, end=end_on, zone=match.group("zone")))
    return stays


def local_now_iso(settings: Settings, day: date | None = None) -> str:
    """Owner-local now, second precision, with the active zone's offset.

    For provenance stamps on manual writes (quick-add, hours logged). `db.now_iso`
    stays UTC for telemetry — but a claim the owner typed tonight in Phoenix must not
    read as tomorrow, and P13's "stored UTC" is satisfied by the explicit offset."""
    zone = active_tz(settings, day or datetime.now().date())
    return datetime.now(ZoneInfo(zone)).replace(microsecond=0).isoformat()


def stays_of(settings: Settings) -> list[Stay]:
    """The configured stays, or none at all if `TZ_RANGES` cannot be parsed.

    `parse_ranges` raises on a malformed line, which is right for a validator and wrong
    for the dozen read paths that call it — a single bad character in `.env` otherwise
    tracebacks out of the day planner, the brief, and `backglass log` alike. Degrading
    here means one unreadable setting costs the *ranges*, not the application, and
    `zone_problems` is what tells the owner it happened.
    """
    try:
        return parse_ranges(settings.tz_ranges)
    except TimezoneError:
        return []


def _zone(name: str) -> ZoneInfo:
    """`ZoneInfo(name)`, raising the same way for every kind of unusable name.

    `ZoneInfo` raises `ZoneInfoNotFoundError` for an unknown zone but `ValueError` for
    a malformed key (`"asia/"` — which `_RANGE` happily accepts). Callers that mean
    "skip a zone I cannot use" have to catch both, and catching only the first is how a
    stale typo still took down `backglass log`.
    """
    return ZoneInfo(name)


def zone_problems(settings: Settings) -> list[str]:
    """Human-readable complaints about `TZ_RANGES`, for `doctor`. Empty when it is fine.

    Config validation belongs here, once, and not in every function that later has to
    survive a bad value. `Settings` does not check zone names, so a typo sits inert
    until the day the stay begins and then breaks the working window, the brief's
    delivery time and every day-boundary query at once.
    """
    try:
        stays = parse_ranges(settings.tz_ranges)
    except TimezoneError as exc:
        return [str(exc)]
    problems = []
    for name in [*(stay.zone for stay in stays), settings.default_tz]:
        try:
            _zone(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            problems.append(f"{name!r} is not a timezone ({exc})")
    return problems


def today_for(settings: Settings, now: datetime | None = None) -> date:
    """The owner's local date, in the zone they are actually in.

    `active_tz` needs a date to pick the zone, and the date depends on the zone, so the
    obvious `active_tz(settings, today_in(default_tz))` is circular: it can only ever
    return the default zone's answer, whatever the ranges say.

    Every zone the owner could be in is tried, and a candidate counts only if it agrees
    with itself: the date it produces must be a date on which `active_tz` picks that same
    zone. Around a stay boundary two candidates can both agree, because `tz_ranges` is
    date-granular and cannot say that a flight lands at 05:00 — at 23:30 UTC the day
    before an eastward stay begins, "Phoenix, the 4th" and "Kolkata, the 5th" are equally
    consistent with the config, and nothing in the data distinguishes "already there"
    from "leaving tomorrow".

    **The tie goes to the earliest date, deliberately.** This function's main caller is
    the future-date guard on `backglass log`, and the two errors are not symmetric: too
    early means a refusal the owner clears by waiting a few hours, while too late lets a
    genuinely future-dated checkpoint through — and the accumulator has no date filter,
    so that inflates every progress bar until the day arrives, with no CLI path to delete
    the row. Preferring the later date was tried and had exactly that effect. A guard
    that is occasionally too strict is a guard; one that is occasionally too loose is not.
    """
    moment = now or datetime.now(UTC)
    zones = [stay.zone for stay in stays_of(settings)]
    zones.append(settings.default_tz)

    everywhere: list[date] = []
    consistent: list[date] = []
    for zone in zones:
        try:
            day = moment.astimezone(_zone(zone)).date()
        except (ZoneInfoNotFoundError, ValueError):
            # An unusable zone name — a typo, a trailing slash — in a stay that may not
            # even be in effect. Skipped here; `doctor` names it.
            continue
        everywhere.append(day)
        if active_tz(settings, day).lower() == zone.lower():
            consistent.append(day)

    # When nothing agrees with itself — which happens on a westward stay's first day,
    # where the default zone's date says the stay has begun and the stay's date says it
    # has not — falling back to the *default* zone's answer silently reinstated the
    # later date and let a future-dated `--on` through, exactly what the tie-break above
    # exists to stop. The floor is the earliest date any candidate zone is in, which can
    # never be later than the owner's real local date.
    pool = consistent or everywhere
    return min(pool) if pool else moment.date()


def local_noon_iso(settings: Settings, day: date) -> str:
    """Local noon on `day`, with that day's active offset.

    The stamp for a write the owner is backdating ("I did four hours on the 14th").
    `local_now_iso` takes a `day` too, but only to choose the zone — it still stamps
    *now*, so passing a past date there silently files the entry under today.

    Noon is the same convention `goals/reviews.py` uses for a day-scoped checkpoint. It
    buys the widest margin available — twelve hours either side of the local midnight,
    so the entry survives being read in UTC — but it is not magic: the owner's two zones
    are 12.5 hours apart, so Kolkata noon rendered in Phoenix is the previous evening
    and Phoenix noon rendered in Kolkata is the following small hours. Readers that care
    about which local day a row belongs to must convert through the zone that was active
    when it was written, exactly as `utc_bounds` does; noon only guarantees that no
    *single-hour* rounding moves it.
    """
    zone = active_tz(settings, day)
    return datetime.combine(day, time(12, 0), ZoneInfo(zone)).isoformat()


def utc_bounds(first: date, last_exclusive: date, tz: str) -> tuple[str, str]:
    """The UTC instants bracketing a span of *local* calendar days.

    This exists because of a trap that is invisible at the call site. Timestamps in
    this ledger deliberately keep their source's own offset (rule 4: "by Friday" in a
    Phoenix email is a Phoenix Friday), so the column holds a mix of `-07:00`,
    `+05:30` and `Z` strings. SQLite's `date()` and `datetime()` silently convert any
    of those to UTC before comparing — so `date(occurred_at) = date('2026-08-01')`
    does NOT mean "on the owner's August 1st". A 17:15 Phoenix event is 00:15 UTC on
    the 2nd and vanishes from the 1st entirely; an 04:00 Kolkata checkpoint lands on
    the previous day. Both are ordinary times of day, not edge cases.

    The fix is to compare *instants*, never rendered dates: convert the local day
    window to UTC here, and in SQL write

        WHERE datetime(occurred_at) >= datetime(:from)
          AND datetime(occurred_at) <  datetime(:to)

    which normalizes both sides to UTC and is therefore correct whatever offset the
    row carries. `last_exclusive` is a local midnight rather than `first + 24h`, so a
    DST transition inside the span cannot shorten or stretch it.
    """
    zone = ZoneInfo(tz)
    start = datetime.combine(first, time.min, tzinfo=zone)
    end = datetime.combine(last_exclusive, time.min, tzinfo=zone)
    return (_as_utc(start), _as_utc(end))


def day_bounds(day: date, tz: str) -> tuple[str, str]:
    """`utc_bounds` for one local day."""
    from datetime import timedelta

    return utc_bounds(day, day + timedelta(days=1), tz)


def local_date_of(utc_value: str, tz: str) -> date:
    """The owner-local calendar date of a UTC instant SQLite handed back.

    The counterpart to `utc_bounds` for aggregates: `MAX(datetime(occurred_at))`
    returns a UTC instant, and the day the owner would call it is a zone conversion,
    not a string slice.
    """
    moment = datetime.fromisoformat(utc_value.replace(" ", "T"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(ZoneInfo(tz)).date()


def _as_utc(moment: datetime) -> str:
    """UTC, second precision, no offset suffix — what `datetime()` compares against."""
    return moment.astimezone(UTC).replace(tzinfo=None, microsecond=0).isoformat()


def active_tz(settings: Settings, day: date) -> str:
    """The zone in force on `day`. Later entries win on overlap.

    Later-wins rather than first-wins because the natural way to record a trip is to
    append it, and a trip appended after an open-ended "I live in Phoenix" entry must
    override it rather than be shadowed by it.
    """
    zone = settings.default_tz
    for stay in stays_of(settings):
        if stay.covers(day):
            zone = stay.zone
    # Never hand back a name that cannot be turned into a zone. Everything downstream —
    # utc_bounds, day_bounds, local_now_iso, the working window — calls ZoneInfo on this
    # string, so a typo in the stay that is currently in effect otherwise raises from
    # whichever of them the caller happened to reach: the dashboard sidebar died in
    # health.risk -> day_bounds. Degrading here keeps a bad TZ_RANGES line costing the
    # *ranges* rather than the application, and `zone_problems` is what names it.
    try:
        _zone(zone)
    except (ZoneInfoNotFoundError, ValueError):
        try:
            _zone(settings.default_tz)
        except (ZoneInfoNotFoundError, ValueError):
            return "UTC"
        return settings.default_tz
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


def window_for(settings: Settings, day: date) -> str:
    """The configured window that governs `day` — weekend or weekday.

    Saturday and Sunday take `weekend_window` when one is set. A weekend is a different
    shape of day, not a shorter workday, and pricing it at weekday hours is what makes a
    planner propose eight hours of reading on a day the owner meant to spend outdoors.
    Blank falls back to `working_window`; whether the day is plannable *at all* remains
    `working_days`'s question, not this one's.
    """
    if day.weekday() >= 5 and settings.weekend_window:
        return settings.weekend_window
    return settings.working_window


def window_on(
    settings: Settings, day: date, window: str | None = None
) -> tuple[datetime, datetime]:
    """The working window as two aware datetimes in the day's active zone.

    P13: the window follows the active timezone. Flying to Coimbatore does not move the
    owner's working day to 21:30; it moves the underlying UTC instants.
    """
    zone = ZoneInfo(active_tz(settings, day))
    start, end = parse_window(window or window_for(settings, day))
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


# ── the 12-hour clock ─────────────────────────────────────────────────────
# Owner ruling 2026-08-06: every surface that prints a time of day prints it the way
# a person reads a clock — "7:30am", never "19:30". One implementation, used by the
# web filters, the schedule canvas, the CLI plan printout and the brief, so noon is
# "12:00pm" everywhere or nowhere.


def clock12(minute_of_day: int) -> str:
    """Minutes past local midnight → '7:30am'."""
    hour, minute = divmod(minute_of_day % (24 * 60), 60)
    return f"{(hour + 11) % 12 + 1}:{minute:02d}{'am' if hour < 12 else 'pm'}"


def hour12(hour: int) -> str:
    """Ruler labels: '8am', '12pm'. No minutes — a ruler is a scale, not a stamp."""
    hour %= 24
    return f"{(hour + 11) % 12 + 1}{'am' if hour < 12 else 'pm'}"


def t12(value: object) -> str:
    """'HH:MM' or a local ISO timestamp → the 12-hour clock."""
    text = str(value)
    hhmm = text[11:16] if len(text) >= 16 and text[10] in "T " else text[:5]
    return clock12(int(hhmm[:2]) * 60 + int(hhmm[3:5]))
