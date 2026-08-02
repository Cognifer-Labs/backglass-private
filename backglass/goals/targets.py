"""Targets and the weekly capacity check. docs/04 §2.2 and §2.3.

  G1  Every active goal has at least one target. A goal with no target is inert and the
      Monday brief says so.
  G2  Targets reset on the configured week start (default Monday, local).
  G3  Target progress is computed from checkpoints, never entered directly.
  G4  A target missed three weeks running is flagged **unrealistic**, and the weekly
      review asks whether to lower it.
  G5  Compute committed hours against available hours every Monday.
  G6  When over capacity, name the gap in hours and list targets by cost, largest first.
      Do not auto-drop anything; the owner chooses.
  G7  When under capacity by more than 25%, say that too. Slack is information.

G4's framing is the load-bearing part: "Lowering a target is a legitimate outcome, and
framing it as one is the difference between a system that gets used and one that generates
guilt." So `unrealistic` is a question, never a verdict, and nothing here lowers anything.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from backglass.config import Settings
from backglass.ledger import USER_ID
from backglass.plan import capacity as capacity_mod
from backglass.plan import timezones


def week_start_of(day: date, week_start: str = "monday") -> date:
    """G2. Targets reset here."""
    offset = day.weekday() if week_start.lower() == "monday" else (day.weekday() + 1) % 7
    return day - timedelta(days=offset)


@dataclass
class TargetProgress:
    target_id: int
    goal_id: int
    goal_title: str
    title: str
    kind: str
    weekly_count: int | None
    estimated_minutes_each: int | None
    done_this_week: int
    missed_weeks: int
    # kind='total' only: the lifetime accumulator (Phase 10). total_count is the
    # number to reach; lifetime_done is SUM(delta) with no week clamp.
    total_count: int | None = None
    lifetime_done: int = 0

    @property
    def complete(self) -> bool:
        if self.kind == "milestone":
            return self.done_this_week > 0
        if self.kind == "total":
            return self.total_count is not None and self.lifetime_done >= self.total_count
        return self.weekly_count is not None and self.done_this_week >= self.weekly_count

    @property
    def weekly_minutes(self) -> int:
        """The implied weekly time cost, for the §2.3 capacity check."""
        if self.kind == "milestone" or not self.weekly_count:
            return 0
        return self.weekly_count * (self.estimated_minutes_each or 0)

    def unrealistic(self, settings: Settings) -> bool:
        """G4. Missed three weeks running — a question for the weekly review."""
        return self.missed_weeks >= settings.unrealistic_after_weeks


def progress(conn: sqlite3.Connection, settings: Settings, day: date) -> list[TargetProgress]:
    """G3. Every count here comes from checkpoints; nothing is entered directly."""
    start = week_start_of(day, settings.week_start)
    tz = timezones.active_tz(settings, day)
    rows = conn.execute(
        "SELECT t.id, t.goal_id, t.kind, t.title, t.weekly_count, t.estimated_minutes_each, "
        "       t.total_count, g.title AS goal_title "
        "FROM target t JOIN goal g ON g.id = t.goal_id "
        "WHERE g.user_id = ? AND g.status = 'active' AND t.active = 1 "
        "ORDER BY g.id, t.id",
        (USER_ID,),
    ).fetchall()

    out: list[TargetProgress] = []
    for row in rows:
        done = count_between(conn, int(row["id"]), start, start + timedelta(days=7), tz)
        out.append(
            TargetProgress(
                target_id=int(row["id"]),
                goal_id=int(row["goal_id"]),
                goal_title=str(row["goal_title"]),
                title=str(row["title"]),
                kind=str(row["kind"]),
                weekly_count=row["weekly_count"],
                estimated_minutes_each=row["estimated_minutes_each"],
                done_this_week=done,
                missed_weeks=_consecutive_misses(conn, settings, row, start),
                total_count=row["total_count"],
                lifetime_done=_lifetime_done(conn, int(row["id"]))
                if row["kind"] == "total"
                else 0,
            )
        )
    return out


def _lifetime_done(conn: sqlite3.Connection, target_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(delta), 0) AS n FROM checkpoint WHERE target_id = ?",
        (target_id,),
    ).fetchone()
    return int(row["n"] or 0)


def count_between(
    conn: sqlite3.Connection, target_id: int, start: date, end: date, tz: str
) -> int:
    """Checkpoints inside a local week, counted by instant.

    `date(occurred_at)` normalized each row to UTC before comparing, so a checkpoint
    logged after ~17:00 in Phoenix counted toward next week and one before ~05:30 in
    Kolkata toward last week — a weekly cadence target was scored against the wrong
    week for ordinary evening work. timezones.utc_bounds explains the mechanism.
    """
    from_utc, to_utc = timezones.utc_bounds(start, end, tz)
    row = conn.execute(
        "SELECT COALESCE(SUM(delta), 0) AS n FROM checkpoint "
        "WHERE target_id = ? AND datetime(occurred_at) >= datetime(?) "
        "  AND datetime(occurred_at) < datetime(?)",
        (target_id, from_utc, to_utc),
    ).fetchone()
    return int(row["n"] or 0)


def _consecutive_misses(
    conn: sqlite3.Connection, settings: Settings, row: dict[str, Any], this_week: date
) -> int:
    """How many *completed* weeks in a row this target was missed.

    The current week is excluded: a target is not missed on Tuesday morning, and counting
    it as missed would flag everything as unrealistic every Monday.
    """
    weekly = row["weekly_count"]
    if not weekly or row["kind"] == "milestone":
        return 0
    misses = 0
    tz = timezones.active_tz(settings, this_week)
    for back in range(1, settings.unrealistic_after_weeks + 1):
        start = this_week - timedelta(days=7 * back)
        if count_between(conn, int(row["id"]), start, start + timedelta(days=7), tz) >= weekly:
            break
        misses += 1
    return misses


def goals_without_targets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """G1. "A goal with no target is inert and the Monday brief says so."."""
    return conn.execute(
        "SELECT g.id, g.title FROM goal g "
        "WHERE g.user_id = ? AND g.status = 'active' "
        "  AND NOT EXISTS (SELECT 1 FROM target t WHERE t.goal_id = g.id AND t.active = 1)",
        (USER_ID,),
    ).fetchall()


# ── §2.3 the capacity check ───────────────────────────────────────────────


@dataclass
class CapacityCheck:
    committed_minutes: int
    available_minutes: int
    by_cost: list[TargetProgress] = field(default_factory=list)

    @property
    def gap_minutes(self) -> int:
        return self.committed_minutes - self.available_minutes

    @property
    def over(self) -> bool:
        return self.gap_minutes > 0

    def slack(self, settings: Settings) -> bool:
        """G7. "When under capacity by more than 25%, say that too. Slack is information."."""
        if self.over or not self.available_minutes:
            return False
        return (self.available_minutes - self.committed_minutes) / self.available_minutes > (
            settings.slack_report_fraction
        )

    def sentence(self, settings: Settings) -> str | None:
        """G6. Name the gap in hours. Do not auto-drop anything; the owner chooses."""
        committed = self.committed_minutes / 60
        available = self.available_minutes / 60
        if self.over:
            return (
                f"Your weekly targets need {committed:.0f} hours. You have "
                f"{available:.0f} available after meetings. Something has to give."
            )
        if self.slack(settings):
            return (
                f"Weekly targets need {committed:.0f} hours against {available:.0f} "
                f"available. {available - committed:.0f} hours spare."
            )
        return None


def weekly_capacity_minutes(
    conn: sqlite3.Connection, settings: Settings, week_start: date
) -> int:
    """G5. Available hours for the coming week, from the same model the planner uses.

    Deliberately the §1.2 capacity, not the raw working window — the whole point of the
    check is that targets are compared against time that actually exists after meetings.
    """
    total = 0
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        if not timezones.is_working_day(settings, day):
            continue
        total += capacity_mod.compute(conn, settings, day).capacity_minutes
    return total


def capacity_check(conn: sqlite3.Connection, settings: Settings, day: date) -> CapacityCheck:
    start = week_start_of(day, settings.week_start)
    targets = progress(conn, settings, day)
    committed = sum(t.weekly_minutes for t in targets)
    return CapacityCheck(
        committed_minutes=committed,
        available_minutes=weekly_capacity_minutes(conn, settings, start),
        # G6: "list targets by cost, largest first".
        by_cost=sorted(targets, key=lambda t: -t.weekly_minutes),
    )
