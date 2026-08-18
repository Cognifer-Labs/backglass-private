"""Is the machinery still running? docs/11 §8.

"The dangerous failure is not the error, it is a brief that looks complete and is not."
A launchd job that stops firing produces exactly that: every panel reads green, the brief
reads confident, and nothing anywhere compares the last run against the cadence it was
supposed to keep. Silence from a source is indistinguishable from silence from the
scheduler unless something says so out loud.

This module is the one thing that says so. It is deliberately a small pure read — a
connection, the settings, the owner's local date, and an injected `now` — so both
surfaces derive the same facts and neither has to own the thresholds. The dashboard turns
them into sidebar alerts, the brief turns them into Attention lines.

Nothing here writes. Staleness is a fact about the clock, not a row.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from backglass import schedule
from backglass.config import Settings
from backglass.ledger import USER_ID
from backglass.plan import timezones

#: How long the plan job is given to finish before its absence is an alert. Generous:
#: the planner makes model calls, and a false alarm at 05:46 trains the owner to ignore
#: the alert entirely, which is the one failure mode worse than not having it.
PLAN_GRACE_MINUTES = 30


@dataclass(frozen=True)
class FailedSource:
    """A source whose credential says it is broken. Named, because an alarm that does
    not name its subject is a mystery rather than a cue."""

    source: str
    status: str
    #: `credential.updated_at` — when the failure was last recorded.
    since: str
    #: Owner-local days between `since` and today. Computed in Python: the ledger's
    #: timestamps carry their source's own offset and SQLite's `date()` silently
    #: converts to UTC first, so a rendered-date subtraction is wrong by a day exactly
    #: when the owner has travelled. See plan/timezones.utc_bounds.
    days: int

    @property
    def phrase(self) -> str:
        if self.days <= 0:
            return "today"
        if self.days == 1:
            return "yesterday"
        return f"{self.days}d ago"


@dataclass(frozen=True)
class Heartbeat:
    """Everything both surfaces need to say "this is older than it looks"."""

    now: datetime
    #: Owner-local date the reading was taken for.
    today: date
    tz: str
    stale_after_hours: float
    #: Newest *completed* run. `finished_at IS NULL` means a run that started and never
    #: came back, which is not evidence that the ledger is current.
    last_run_id: int | None = None
    last_run_at: str | None = None
    age_hours: float | None = None
    failed_sources: list[FailedSource] = field(default_factory=list)
    #: True once the plan job's window plus its grace has passed on a working day.
    plan_due: bool = False
    plan_id: int | None = None

    @property
    def never_ran(self) -> bool:
        return self.last_run_id is None

    @property
    def stale(self) -> bool:
        """A sync older than the threshold. Never-ran is reported on its own — "0h ago"
        and "never" are different problems with different fixes."""
        return self.age_hours is not None and self.age_hours >= self.stale_after_hours

    @property
    def plan_missing(self) -> bool:
        return self.plan_due and self.plan_id is None

    @property
    def age_phrase(self) -> str:
        """The age in the units the reader thinks in. Matches panels.relative's
        thresholds so the sidebar and the brief cannot disagree about the same run."""
        hours = int(self.age_hours or 0)
        return f"{hours}h ago" if hours < 48 else f"{hours // 24}d ago"

    @property
    def failed_phrase(self) -> str | None:
        """"Gmail failing since Thu" — the names and the age, or None when all is well."""
        if not self.failed_sources:
            return None
        names = ", ".join(s.source for s in self.failed_sources)
        oldest = max(self.failed_sources, key=lambda s: s.days)
        return f"{names} failing since {oldest.phrase}"


def read(
    conn: sqlite3.Connection,
    settings: Settings,
    today: date,
    now: datetime | None = None,
) -> Heartbeat:
    """The staleness facts as of `now`. `now` is injectable so a test never reads a
    wall clock and a caller never has to guess which zone this wants."""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    tz = timezones.active_tz(settings, today)

    # Only sync runs. `run` records every job kind — roadmap/interview.py writes
    # kind='interview' — and counting all of them meant one roadmap interview reset the
    # clock on all three surfaces, so a sync that had been dead five hours read green on
    # the dashboard, in the brief, and in doctor. The surfaces say "last sync ran", so
    # the query has to mean it.
    #
    # Ordered by the instant, not the string: run rows are written UTC by db.now_iso,
    # but datetime() normalizes whatever offset a future writer hands it.
    run = conn.execute(
        "SELECT id, finished_at FROM run WHERE user_id = ? AND finished_at IS NOT NULL "
        "AND kind = 'sync' "
        "ORDER BY datetime(finished_at) DESC, id DESC LIMIT 1",
        (USER_ID,),
    ).fetchone()
    last_run_id = int(run["id"]) if run else None
    last_run_at = str(run["finished_at"]) if run else None
    age_hours = None
    if last_run_at:
        elapsed = (moment - _as_aware(last_run_at)).total_seconds() / 3600
        age_hours = max(0.0, elapsed)

    # A paused source is not a failing one — the owner chose the silence. Same rule as
    # the Sources panel and brief_source_health.sql.
    failed = [
        FailedSource(
            source=str(row["source"]),
            status=str(row["status"]),
            since=str(row["updated_at"]),
            days=(today - timezones.local_date_of(str(row["updated_at"]), tz)).days,
        )
        for row in conn.execute(
            "SELECT source, status, updated_at FROM credential "
            "WHERE user_id = ? AND status != 'ok' AND enabled = 1 ORDER BY updated_at ASC",
            (USER_ID,),
        ).fetchall()
    ]

    plan = conn.execute(
        "SELECT id FROM day_plan WHERE user_id = ? AND local_date = ? "
        "AND status != 'superseded' ORDER BY id DESC LIMIT 1",
        (USER_ID, today.isoformat()),
    ).fetchone()

    return Heartbeat(
        now=moment,
        today=today,
        tz=tz,
        stale_after_hours=float(settings.sync_stale_after_hours),
        last_run_id=last_run_id,
        last_run_at=last_run_at,
        age_hours=age_hours,
        failed_sources=failed,
        plan_due=_plan_due(settings, today, moment, tz),
        plan_id=int(plan["id"]) if plan else None,
    )


def _plan_due(settings: Settings, today: date, moment: datetime, tz: str) -> bool:
    """Whether today's plan should exist by now.

    Two gates, both about not crying wolf. The working day: docs/04 P3 says a day under
    60 minutes of capacity gets no plan at all, so a weekend legitimately has none and
    alerting every Saturday teaches the owner to skip the block. And the current day:
    "the planner did not run" is actionable this morning and is merely history on a
    brief regenerated for last Tuesday, so a past or future date never raises it.
    """
    if not timezones.is_working_day(settings, today):
        return False
    zone = ZoneInfo(tz)
    if moment.astimezone(zone).date() != today:
        return False
    # Read from the setting the launchd template is rendered from, never from a copy.
    # This was `time(5, 45)` with a comment saying the template fires then — true when it
    # was written and false the moment `plan_at` became configurable, at which point
    # changing the hour would have moved the job and left the alarm watching the old one.
    # It is the same two-copies failure `schedule.render` exists to end, one module over.
    hour, minute = schedule._hhmm(settings.plan_at)
    deadline = datetime.combine(today, time(hour, minute), tzinfo=zone) + timedelta(
        minutes=PLAN_GRACE_MINUTES
    )
    return moment >= deadline


def _as_aware(value: str) -> datetime:
    """A stored timestamp as an instant. Naive values are UTC — db.now_iso always
    writes an offset, but a hand-inserted fixture row may not."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
