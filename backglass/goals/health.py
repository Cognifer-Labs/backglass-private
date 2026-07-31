"""Staleness and risk. docs/04 §2.5.

    "Two different signals, and conflating them is a common mistake."

    **Stale** is about activity: days since the last checkpoint on any target of this goal.
    **At risk** is about trajectory: at the current rate, will this goal reach its
    definition of done by its target date?

    "A goal can be fresh and at risk (lots of activity, not enough to finish in time). It
    can be stale and on track (ahead of schedule, took a week off). These need different
    responses."

  G11  Compute staleness and risk independently. Never merge into one "health" score.
  G12  Staleness shows as a chip with the day count: "12 days quiet." Never a bare colour.
  G13  Risk shows as a projected completion date against the target date, in words:
       "on pace for 14 Oct, target is 30 Sep."
  G14  A goal at risk for two consecutive weeks gets one direct question in the Monday
       brief: extend the date, cut the scope, or raise the weekly target.

G11 is why this module returns two separate objects and has no function that combines
them. The temptation to emit a single 0–100 "goal health" is real and it destroys the
distinction the section exists to draw — the two states need different responses, and a
merged number can only prompt one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from backglass.config import Settings
from backglass.goals import targets as targets_mod
from backglass.ledger import USER_ID


def _word_date(day: date, *, with_year: bool = False) -> str:
    return day.strftime("%-d %b %Y") if with_year else day.strftime("%-d %b")


# ── staleness: activity ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Staleness:
    goal_id: int
    goal_title: str
    days_quiet: int | None  # None when there has never been a checkpoint
    level: str  # fresh | warn | serious

    def chip(self) -> str:
        """G12. A day count, never a bare colour."""
        if self.days_quiet is None:
            return "no checkpoints yet"
        return f"{self.days_quiet} day{'' if self.days_quiet == 1 else 's'} quiet"


def staleness(conn: sqlite3.Connection, settings: Settings, day: date) -> list[Staleness]:
    rows = conn.execute(
        "SELECT g.id, g.title, "
        "  (SELECT MAX(date(cp.occurred_at)) FROM checkpoint cp "
        "   JOIN target t ON t.id = cp.target_id WHERE t.goal_id = g.id) AS last_at "
        "FROM goal g WHERE g.user_id = ? AND g.status = 'active' ORDER BY g.id",
        (USER_ID,),
    ).fetchall()

    out: list[Staleness] = []
    for row in rows:
        if row["last_at"]:
            # Clamped at zero: occurred_at is stored UTC, so a checkpoint logged
            # tonight can carry tomorrow's date in a western timezone — and
            # "-1 days quiet" is an impossible claim on a provenance surface.
            days: int | None = max(
                0, (day - date.fromisoformat(str(row["last_at"]))).days
            )
        else:
            days = None
        if days is None or days >= settings.stale_serious_days:
            level = "serious"
        elif days >= settings.stale_warn_days:
            level = "warn"
        else:
            level = "fresh"
        out.append(
            Staleness(
                goal_id=int(row["id"]),
                goal_title=str(row["title"]),
                days_quiet=days,
                level=level,
            )
        )
    return out


# ── risk: trajectory ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Risk:
    goal_id: int
    goal_title: str
    target_date: date | None
    required_per_week: float
    observed_per_week: float
    projected_completion: date | None
    at_risk: bool

    def sentence(self) -> str | None:
        """G13. In words, as a projected date against the target date.

        The year is included whenever the two dates fall in different years. G13's own
        example ("on pace for 14 Oct, target is 30 Sep") omits it, but a projection that
        slips into next year rendered as "5 Jun" reads as the *past* against a "28 Sep"
        target — the sentence that exists to convey slippage would hide its size.
        """
        if self.target_date is None:
            return None
        if self.projected_completion is None:
            return (
                f"{self.goal_title}: no observed progress, target is "
                f"{_word_date(self.target_date)}."
            )
        cross_year = self.projected_completion.year != self.target_date.year
        return (
            f"{self.goal_title}: on pace for "
            f"{_word_date(self.projected_completion, with_year=cross_year)}, target is "
            f"{_word_date(self.target_date, with_year=cross_year)}."
        )


def risk(conn: sqlite3.Connection, settings: Settings, day: date) -> list[Risk]:
    """Required rate versus observed rate over the trailing four weeks (docs/04 §2.5).

    "Required" is the remaining cadence work divided by the weeks left to the target date.
    "Observed" is checkpoints per week over the trailing window. A goal is at risk when it
    cannot finish in time at the rate it is actually moving — which is a different
    question from whether it moved recently, and that is the whole point of §2.5.
    """
    window_days = settings.risk_window_weeks * 7
    since = day - timedelta(days=window_days)

    rows = conn.execute(
        "SELECT g.id, g.title, g.target_date, "
        "  (SELECT COALESCE(SUM(cp.delta), 0) FROM checkpoint cp "
        "   JOIN target t ON t.id = cp.target_id "
        "   WHERE t.goal_id = g.id AND date(cp.occurred_at) >= date(?)) AS recent, "
        "  (SELECT COALESCE(SUM(cp.delta), 0) FROM checkpoint cp "
        "   JOIN target t ON t.id = cp.target_id WHERE t.goal_id = g.id) AS total, "
        "  (SELECT COALESCE(SUM(t.weekly_count), 0) FROM target t "
        "   WHERE t.goal_id = g.id AND t.active = 1 AND t.kind = 'cadence') AS weekly_needed, "
        "  (SELECT COALESCE(SUM(MAX(t.total_count - COALESCE("
        "     (SELECT SUM(cp.delta) FROM checkpoint cp WHERE cp.target_id = t.id), 0), 0)), 0) "
        "   FROM target t WHERE t.goal_id = g.id AND t.active = 1 "
        "   AND t.kind = 'total' AND t.total_count IS NOT NULL) AS totals_remaining "
        "FROM goal g WHERE g.user_id = ? AND g.status = 'active' ORDER BY g.id",
        (since.isoformat(), USER_ID),
    ).fetchall()

    out: list[Risk] = []
    for row in rows:
        target_date = (
            date.fromisoformat(str(row["target_date"])) if row["target_date"] else None
        )
        observed = float(row["recent"] or 0) / settings.risk_window_weeks
        weekly_needed = float(row["weekly_needed"] or 0)

        if target_date is None or target_date <= day:
            weeks_left = 0.0
        else:
            weeks_left = (target_date - day).days / 7

        # What still has to happen: the cadence the goal claims it needs for the weeks
        # that remain, plus whatever its lifetime totals still lack (Phase 10). The
        # observed rate already counts total-target checkpoints — SUM(delta) is blind
        # to kind — so both sides of the projection speak the same units.
        remaining = weekly_needed * weeks_left + float(row["totals_remaining"] or 0)
        required = (remaining / weeks_left) if weeks_left else 0.0

        if observed > 0 and remaining > 0:
            weeks_to_finish = remaining / observed
            projected: date | None = day + timedelta(days=int(round(weeks_to_finish * 7)))
        elif remaining <= 0:
            projected = day
        else:
            projected = None

        at_risk = bool(
            target_date is not None
            and remaining > 0
            and (projected is None or projected > target_date)
        )
        out.append(
            Risk(
                goal_id=int(row["id"]),
                goal_title=str(row["title"]),
                target_date=target_date,
                required_per_week=required,
                observed_per_week=observed,
                projected_completion=projected,
                at_risk=at_risk,
            )
        )
    return out


def at_risk_goal_ids(conn: sqlite3.Connection, settings: Settings, day: date) -> set[int]:
    """Feeds the planner's `advances an at-risk goal` priority tier (docs/04 §1.5)."""
    return {r.goal_id for r in risk(conn, settings, day) if r.at_risk}


def sustained_risk(conn: sqlite3.Connection, settings: Settings, day: date) -> list[Risk]:
    """G14. At risk for two consecutive weeks — one direct question in the Monday brief.

    Checked by recomputing risk as it stood a week ago. Storing a weekly snapshot would be
    faster, but it would also be a second source of truth for something that is already
    derivable, and the number of goals is single digits.
    """
    now_risky = {r.goal_id: r for r in risk(conn, settings, day) if r.at_risk}
    then_risky = {r.goal_id for r in risk(conn, settings, day - timedelta(days=7)) if r.at_risk}
    return [r for goal_id, r in now_risky.items() if goal_id in then_risky]


def question_for(goal: Risk) -> str:
    """G14's question, with the three legitimate answers named.

    Naming all three matters: docs/04 §2.2 G4 makes the point that lowering a target is a
    legitimate outcome, and a question that only offers "work harder" is the guilt-
    generating version of this feature.
    """
    return (
        f"{goal.goal_title} has been at risk for two weeks. "
        "Extend the date, cut the scope, or raise the weekly target?"
    )


def unrealistic_targets(
    conn: sqlite3.Connection, settings: Settings, day: date
) -> list[targets_mod.TargetProgress]:
    """G4, surfaced for the Monday review."""
    return [t for t in targets_mod.progress(conn, settings, day) if t.unrealistic(settings)]
