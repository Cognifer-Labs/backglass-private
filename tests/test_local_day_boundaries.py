"""A local day is a span of instants, not a rendered date. CLAUDE.md rule 4, one layer down.

Every timestamp in this ledger keeps its source's own UTC offset, because "by Friday" in
a Phoenix email means a Phoenix Friday. SQLite's `date()` and `datetime()` convert those
strings to UTC before comparing, so `date(occurred_at) = date('2026-08-01')` asks a
question nobody meant: a 17:15 Phoenix event is 00:15 UTC on the 2nd and falls out of its
own day, and an 04:00 Kolkata checkpoint lands on the day before.

None of these are edge cases — they are evening and early-morning, which is when a
founder logs work. Each test below fails against `date(...)` comparison and passes
against instant comparison; the times were chosen to sit inside the broken window, not
merely near it.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from zoneinfo import ZoneInfo

import pytest

from backglass.config import Settings
from backglass.db import now_iso
from backglass.goals import checkpoints, health, targets
from backglass.ledger import USER_ID
from backglass.plan import capacity, timezones

PHOENIX_DAY = date(2026, 8, 1)  # a Saturday; the week starts Monday 2026-07-27


@pytest.fixture
def kolkata(settings: Settings) -> Settings:
    """The same owner, mid-trip. +05:30 is the offset that breaks half-hour assumptions."""
    return settings.model_copy(update={"default_tz": "Asia/Kolkata"})


def _calendar_event(conn: sqlite3.Connection, starts: str, ends: str, title: str) -> None:
    payload = f'{{"starts_at": "{starts}", "ends_at": "{ends}", "title": "{title}"}}'
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, raw_json, content_hash) VALUES (?, 'calendar:primary', ?, ?, ?, ?, ?, ?)",
        (USER_ID, f"ev-{title}", now_iso(), starts, title, payload, f"h-{title}"),
    )
    conn.commit()


def _goal_with_cadence(conn: sqlite3.Connection, weekly: int = 2) -> int:
    conn.execute(
        "INSERT INTO goal (user_id, title, horizon, target_date, definition_of_done,"
        " status, created_at) VALUES (?, 'Ship it', 'quarterly', '2026-09-30', 'shipped',"
        " 'active', '2026-07-01T00:00:00Z')",
        (USER_ID,),
    )
    goal_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO target (goal_id, kind, title, weekly_count, active, created_at)"
        " VALUES (?, 'cadence', 'sessions', ?, 1, '2026-07-01T00:00:00Z')",
        (goal_id, weekly),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


# ── the planner's view of the day ─────────────────────────────────────────


def test_an_evening_phoenix_meeting_stays_on_its_own_day(conn: sqlite3.Connection) -> None:
    """The consequence of getting this wrong is the planner booking work over a meeting.

    17:15 in Phoenix is 00:15 UTC the next day, so `date(occurred_at)` filed this event
    under 2 August and left 1 August looking free.
    """
    _calendar_event(
        conn, "2026-08-01T17:15:00-07:00", "2026-08-01T18:00:00-07:00", "board call"
    )
    events = capacity.fixed_events(conn, PHOENIX_DAY, "America/Phoenix")
    assert [e.title for e in events] == ["board call"]


def test_an_early_kolkata_meeting_is_not_pulled_into_yesterday(
    conn: sqlite3.Connection,
) -> None:
    """The mirror case: 04:00 +05:30 is 22:30 UTC on the day before."""
    _calendar_event(
        conn, "2026-08-01T04:00:00+05:30", "2026-08-01T05:00:00+05:30", "standup"
    )
    assert capacity.fixed_events(conn, PHOENIX_DAY, "Asia/Kolkata")
    assert not capacity.fixed_events(conn, date(2026, 7, 31), "Asia/Kolkata")


def test_a_midday_event_was_never_the_problem(conn: sqlite3.Connection) -> None:
    """Guards the guard: if this ever fails, the window moved, not the timezone logic."""
    _calendar_event(
        conn, "2026-08-01T11:00:00-07:00", "2026-08-01T12:00:00-07:00", "lunch"
    )
    assert [e.title for e in capacity.fixed_events(conn, PHOENIX_DAY, "America/Phoenix")] == [
        "lunch"
    ]


# ── goal accounting ───────────────────────────────────────────────────────


def test_an_evening_checkpoint_counts_toward_this_week_not_next(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """A 22:00 Phoenix session is 05:00 UTC tomorrow — it used to score the wrong week.

    Weekly cadence is the number the goals page and the brief both report, so the error
    showed up as "0/2 this week" on an evening the owner had just done the work.
    """
    target_id = _goal_with_cadence(conn, weekly=2)
    checkpoints.record(
        conn, target_id, source="manual", occurred_at="2026-08-01T22:00:00-07:00"
    )
    conn.commit()
    rows = {t.target_id: t for t in targets.progress(conn, settings, PHOENIX_DAY)}
    assert rows[target_id].done_this_week == 1


def test_a_checkpoint_after_the_local_week_ends_does_not_leak_backwards(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Sunday 23:30 Phoenix is Monday UTC: it belongs to the week that just ended."""
    target_id = _goal_with_cadence(conn)
    checkpoints.record(
        conn, target_id, source="manual", occurred_at="2026-08-02T23:30:00-07:00"
    )
    conn.commit()
    this_week = {t.target_id: t for t in targets.progress(conn, settings, PHOENIX_DAY)}
    next_week = {t.target_id: t for t in targets.progress(conn, settings, date(2026, 8, 4))}
    assert this_week[target_id].done_this_week == 1
    assert next_week[target_id].done_this_week == 0


def test_work_logged_tonight_is_zero_days_quiet(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Staleness drives the NEEDS ATTENTION list; a stale chip on work done hours ago is
    the fastest way to teach the owner to stop trusting the chips."""
    target_id = _goal_with_cadence(conn)
    checkpoints.record(
        conn, target_id, source="manual", occurred_at="2026-08-01T21:00:00-07:00"
    )
    conn.commit()
    quiet = {
        s.goal_title: s.days_quiet
        for s in health.staleness(conn, settings, PHOENIX_DAY)
    }
    assert quiet["Ship it"] == 0


def test_staleness_takes_the_latest_instant_across_mixed_offsets(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """MAX over rendered dates is not MAX over time when the rows carry different zones.

    Both of these checkpoints happened on 1 August in Phoenix, so on the 2nd the goal has
    been quiet for a day. The old query took MAX of the *rendered* dates, where the
    Kolkata stamp reads "2026-08-02" despite happening ~12 hours earlier than the Phoenix
    one — and reported 0 days quiet, crediting the owner with work they had not done that
    day. Understating staleness is the dangerous direction: it hides a goal that should
    have surfaced in NEEDS ATTENTION.
    """
    target_id = _goal_with_cadence(conn)
    checkpoints.record(
        conn, target_id, source="manual", occurred_at="2026-08-02T00:10:00+05:30"
    )
    checkpoints.record(
        conn, target_id, source="manual", occurred_at="2026-08-01T23:50:00-07:00"
    )
    conn.commit()
    quiet = {
        s.goal_title: s.days_quiet
        for s in health.staleness(conn, settings, date(2026, 8, 2))
    }
    assert quiet["Ship it"] == 1


# ── the primitive itself ──────────────────────────────────────────────────


def test_the_window_is_a_pair_of_utc_instants() -> None:
    first, last = timezones.day_bounds(PHOENIX_DAY, "America/Phoenix")
    assert (first, last) == ("2026-08-01T07:00:00", "2026-08-02T07:00:00")
    first_in, last_in = timezones.day_bounds(PHOENIX_DAY, "Asia/Kolkata")
    assert (first_in, last_in) == ("2026-07-31T18:30:00", "2026-08-01T18:30:00")


def test_a_dst_day_is_still_one_local_day() -> None:
    """Phoenix has no DST, but the owner's zone list is editable and this must not care.

    The bounds come from two local midnights, so a 23- or 25-hour day is exactly one day.
    """
    spring = date(2026, 3, 8)  # US DST begins
    first, last = timezones.day_bounds(spring, "America/New_York")
    assert (first, last) == ("2026-03-08T05:00:00", "2026-03-09T04:00:00")


def test_local_date_of_reads_an_instant_back_into_the_owners_day() -> None:
    assert timezones.local_date_of("2026-08-02 04:00:00", "America/Phoenix") == date(2026, 8, 1)
    assert timezones.local_date_of("2026-07-31T20:00:00", "Asia/Kolkata") == date(2026, 8, 1)


def test_the_roadmap_page_counts_the_same_week_the_goals_page_does(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """The last surviving `date(occurred_at)` bucket, found re-auditing after the fix.

    `targets.count_between` was corrected for the goals page, but the roadmap page kept
    a private copy of the same query that still compared rendered dates — so a 22:00
    Phoenix session showed 1/2 on /goals and 0/2 on the roadmap for the same week. Two
    surfaces disagreeing about the same number is worse than both being wrong: it is the
    thing that makes an owner stop believing either. There is now one implementation.
    """
    from backglass.web.routes.roadmaps import progress_context

    target_id = _goal_with_cadence(conn, weekly=2)
    # Sunday 22:00 Phoenix is Monday 05:00 UTC — the last hours of the local week,
    # rendered by date() as the first day of the next one. Times inside the broken
    # window, not merely near it: this test fails against the old private copy.
    checkpoints.record(
        conn, target_id, source="manual", occurred_at="2026-08-02T22:00:00-07:00"
    )
    conn.commit()
    goal_id = int(
        conn.execute("SELECT goal_id AS id FROM target WHERE id = ?", (target_id,))
        .fetchone()["id"]
    )
    cadences = conn.execute(
        "SELECT id AS target_id, title, weekly_count FROM target WHERE id = ?",
        (target_id,),
    ).fetchall()
    detail = progress_context(
        conn,
        settings,
        PHOENIX_DAY,
        {"r": {"goal_id": goal_id}, "steps": [], "cadences": cadences},
    )

    on_goals_page = {t.target_id: t for t in targets.progress(conn, settings, PHOENIX_DAY)}
    assert detail["cadences"][0]["done_this_week"] == 1
    assert detail["cadences"][0]["done_this_week"] == on_goals_page[target_id].done_this_week


def test_the_roadmap_cadence_count_follows_the_owner_to_kolkata(
    conn: sqlite3.Connection, kolkata: Settings
) -> None:
    """+05:30's failure is the mirror image: 04:00 local is the *previous* UTC day."""
    from backglass.web.routes.roadmaps import progress_context

    target_id = _goal_with_cadence(conn, weekly=2)
    # Monday 04:00 Kolkata is Sunday 22:30 UTC — the first hours of the local week,
    # rendered by date() as the last day of the week before, so the old copy scored it
    # zero on the very morning the work was done.
    checkpoints.record(
        conn, target_id, source="manual", occurred_at="2026-07-27T04:00:00+05:30"
    )
    conn.commit()
    goal_id = int(
        conn.execute("SELECT goal_id AS id FROM target WHERE id = ?", (target_id,))
        .fetchone()["id"]
    )
    cadences = conn.execute(
        "SELECT id AS target_id, title, weekly_count FROM target WHERE id = ?",
        (target_id,),
    ).fetchall()
    detail = progress_context(
        conn,
        kolkata,
        date(2026, 7, 27),
        {"r": {"goal_id": goal_id}, "steps": [], "cadences": cadences},
    )
    assert detail["cadences"][0]["done_this_week"] == 1


def test_a_backdated_stamp_lands_on_the_day_it_names_in_both_zones(
    settings: Settings, kolkata: Settings
) -> None:
    """`local_now_iso` takes a day only to pick the zone — it always stamps *now*, so
    a backdated write (`backglass log --on`) needed its own helper. Noon is chosen so
    no later reader in either zone can round the entry onto a neighbouring day."""
    day = date(2026, 9, 14)

    phoenix = timezones.local_noon_iso(settings, day)
    india = timezones.local_noon_iso(kolkata, day)

    assert phoenix == "2026-09-14T12:00:00-07:00"
    assert india == "2026-09-14T12:00:00+05:30"
    # The point of noon: converted to UTC, both are still the 14th.
    for stamp in (phoenix, india):
        assert timezones.local_date_of(stamp, "UTC") == day


def test_today_is_the_date_in_the_zone_the_owner_is_actually_in() -> None:
    """`active_tz` needs a date to pick the zone and the date depends on the zone.

    Resolving that circularity by starting from `default_tz` gets the *arrival* day of a
    stay wrong: the Phoenix-derived date still falls before the range begins, so the
    range does not apply, and for the 12.5 hours between Kolkata midnight and 12:30 the
    owner's genuine today reads as tomorrow. `today_for` iterates to a fixed point.

    Settings is built rather than `model_copy`-ed because `tz_ranges` is normalized by a
    field validator that model_copy skips — the 2026-07-30 lesson, one layer down.
    """
    from datetime import UTC as _utc
    from datetime import datetime as _dt

    def _with(ranges: list[str]) -> Settings:
        return Settings(
            _env_file=None,  # type: ignore[call-arg]
            default_tz="America/Phoenix",
            alt_tz="Asia/Kolkata",
            tz_ranges=ranges,
        )

    # 23:30 UTC on Aug 2 = 16:30 Phoenix (still the 2nd) = 05:00 Kolkata on the 3rd.
    moment = _dt(2026, 8, 2, 23, 30, tzinfo=_utc)

    assert timezones.today_for(_with([]), moment) == date(2026, 8, 2)
    # Mid-stay, unambiguous: only Kolkata agrees with itself.
    assert timezones.today_for(
        _with(["2026-07-01..2026-08-20:Asia/Kolkata"]), moment
    ) == date(2026, 8, 3)

    # ── the boundary, where two answers are equally consistent ──────────────
    #
    # `tz_ranges` is date-granular; it cannot say a flight lands at 05:00. At this
    # instant "Phoenix, the 2nd" and "Kolkata, the 3rd" both satisfy the
    # self-consistency test, and the config genuinely does not distinguish "already
    # arrived" from "leaving tomorrow". The tie goes to the earlier date because the
    # caller is a future-date guard: too early costs the owner a wait, too late lets a
    # future-dated checkpoint into an accumulator that has no date filter and no delete.
    arriving = _with(["2026-08-03..2026-08-20:Asia/Kolkata"])
    assert timezones.today_for(arriving, moment) == date(2026, 8, 2)

    # The same shape one day out from a later stay — the case that regressed when the
    # tie went the other way, letting `--on 2026-08-05` through as "not future".
    departing = _with(["2026-08-05..2026-08-20:Asia/Kolkata"])
    pre_departure = _dt(2026, 8, 4, 19, 0, tzinfo=_utc)  # Phoenix noon on the 4th
    assert timezones.today_for(departing, pre_departure) == date(2026, 8, 4)


def test_a_westward_stay_never_reports_a_day_that_has_not_started() -> None:
    """The fallback path used to skip the tie-break entirely.

    When no candidate zone agrees with itself — which is what happens on the first day
    of a stay *west* of `default_tz` — the function returned the default zone's date,
    which is the later one for that direction. `--on` then accepted a date 24.5 hours
    ahead of the real instant. The floor is now the earliest date any candidate zone is
    in, which can never be later than the owner's real local date.
    """
    from datetime import UTC as _utc
    from datetime import datetime as _dt

    # Home is Kolkata; the stay is westward, to Phoenix, starting on the 3rd.
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        default_tz="Asia/Kolkata",
        tz_ranges=["2026-08-03..2026-08-20:America/Phoenix"],
    )
    # 18:30Z = 00:00 on the 3rd in Kolkata, but still 11:30 on the 2nd in Phoenix.
    moment = _dt(2026, 8, 2, 18, 30, tzinfo=_utc)

    today = timezones.today_for(settings, moment)

    assert today == date(2026, 8, 2)
    # The property that matters, stated directly: whatever zone this answer implies, the
    # day must actually have begun there.
    assert moment.astimezone(ZoneInfo(timezones.active_tz(settings, today))).date() >= today


def test_an_unusable_zone_name_is_skipped_however_it_is_malformed() -> None:
    """`ZoneInfo` raises ZoneInfoNotFoundError for an unknown zone but ValueError for a
    malformed key, and `_RANGE` accepts a trailing slash. Catching only the first left
    `Asia/` — one character from the typo this guard was added for — still tracebacking
    out of `backglass log`."""
    from datetime import UTC as _utc
    from datetime import datetime as _dt

    moment = _dt(2026, 8, 4, 19, 0, tzinfo=_utc)
    for bad in ("Asia/Kolkatta", "Asia/", "Nowhere/Atall"):
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            default_tz="America/Phoenix",
            tz_ranges=[f"2026-01-01..2026-01-10:{bad}"],
        )
        assert timezones.today_for(settings, moment) == date(2026, 8, 4), bad


def test_a_malformed_range_line_does_not_raise_from_today_for() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        default_tz="America/Phoenix",
        tz_ranges=["not-a-range"],
    )
    from datetime import UTC as _utc
    from datetime import datetime as _dt

    assert timezones.today_for(
        settings, _dt(2026, 8, 4, 19, 0, tzinfo=_utc)
    ) == date(2026, 8, 4)


def test_doctor_is_where_a_bad_timezone_config_gets_reported() -> None:
    """The write path degrades silently; the preflight is what says why."""
    ok = Settings(
        _env_file=None,  # type: ignore[call-arg]
        default_tz="America/Phoenix",
        tz_ranges=["2026-08-03..2026-08-20:Asia/Kolkata"],
    )
    assert timezones.zone_problems(ok) == []

    for bad, expect in (
        (["2026-01-01..2026-01-10:Asia/Kolkatta"], "not a timezone"),
        (["2026-01-01..2026-01-10:Asia/"], "not a timezone"),
        (["not-a-range"], "bad TZ_RANGES entry"),
    ):
        broken = Settings(
            _env_file=None,  # type: ignore[call-arg]
            default_tz="America/Phoenix",
            tz_ranges=bad,
        )
        problems = timezones.zone_problems(broken)
        assert problems and expect in problems[0], bad


def test_a_stale_timezone_typo_cannot_break_logging(settings: Settings) -> None:
    """`Settings` does not validate zone names, and a `TZ_RANGES` line outlives the trip
    it described. A typo in a stay that is not even in effect must not raise out of
    `backglass log` — the ledger's primary write path."""
    from datetime import UTC as _utc
    from datetime import datetime as _dt

    typo = Settings(
        _env_file=None,  # type: ignore[call-arg]
        default_tz="America/Phoenix",
        tz_ranges=["2026-01-01..2026-01-10:Asia/Kolkatta"],
    )
    assert timezones.today_for(typo, _dt(2026, 8, 4, 19, 0, tzinfo=_utc)) == date(2026, 8, 4)
