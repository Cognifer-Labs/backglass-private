"""The Goals page: goal cards with their targets, and the checklist as a week grid.

docs/04 §2. The dashboard panels stay the at-a-glance summary; this page is the deep
view. Two deliberate framings from the spec shape it:

- Goals are cards grouped by goal, not a flat target list, because staleness and risk
  are goal-level signals (G11–G13) and the definition of done belongs next to the
  progress it defines ("a goal without one is a mood").
- The checklist renders as the current week, not just today, because a habit is a
  row over days. Streaks show as a bare count (C4: quiet), and the only interactive
  cell is today — the past is a record, not an editing surface.

The replan (2026-07) reorders the page around what the morning actually needs, without
changing any of that reasoning:

- TODAY'S TICKS comes first: the week grid reduced to its today column, one 24px box
  per item scheduled today, bounded at seven by C1. It is the page's first swap target.
- THIS WEEK is a `<details>` whose wrapper lives in goals.html OUTSIDE every swap
  target, so the fold survives a tick structurally — the tick response replaces
  #today-ticks and refreshes the inner #week-grid out of band, and never touches the
  details element. No server-rendered `open` echo is built or needed.
- The three-reel KPI strip is retired: its only live number, "targets on pace", moves
  into the THIS WEEK summary as #wk-pace, and the other two duplicated the banner.
- Cards split into NEEDS ATTENTION (stale warn/serious, at risk, or behind on a
  cadence this week) and ON TRACK. A flagged goal renders the full card; a healthy one
  collapses to a single row that discloses the same card.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from backglass.config import Settings
from backglass.db import query
from backglass.goals import checklist, checkpoints, health
from backglass.goals import targets as targets_mod
from backglass.ledger import USER_ID
from backglass.plan import timezones
from backglass.web import actions
from backglass.web.panels import week_start_of
from backglass.web.params import RowId


@dataclass(frozen=True)
class Cell:
    day: date
    scheduled: bool
    ticked: bool
    is_today: bool
    is_future: bool


@dataclass(frozen=True)
class GridRow:
    item_id: int
    title: str
    streak: int
    cells: list[Cell]

    @property
    def today_cell(self) -> Cell | None:
        return next((c for c in self.cells if c.is_today), None)


@dataclass(frozen=True)
class TodayRow:
    """One row of TODAY'S TICKS — the week grid's today column, on its own."""

    item_id: int
    title: str
    streak: int
    ticked: bool


@dataclass(frozen=True)
class WeekGrid:
    days: list[date]
    today: date
    rows: list[GridRow]
    done_today: int
    total_today: int

    @property
    def empty(self) -> bool:
        return not self.rows

    @property
    def today_rows(self) -> list[TodayRow]:
        """Items scheduled today. C1 caps the checklist at seven, so this list is
        bounded by the same rule that bounds the grid — no separate truncation."""
        out = []
        for r in self.rows:
            cell = r.today_cell
            if cell is not None and cell.scheduled:
                out.append(
                    TodayRow(
                        item_id=r.item_id, title=r.title, streak=r.streak, ticked=cell.ticked
                    )
                )
        return out


def week_grid(conn: sqlite3.Connection, settings: Settings, today: date) -> WeekGrid:
    start = week_start_of(today, settings.week_start)
    days = [start + timedelta(days=i) for i in range(7)]
    items = conn.execute(
        "SELECT id, title, weekday_mask FROM checklist_item "
        "WHERE user_id = ? AND active = 1 ORDER BY sort_order, id",
        (USER_ID,),
    ).fetchall()
    ticked = {
        (int(r["checklist_item_id"]), str(r["local_date"]))
        for r in conn.execute(
            "SELECT checklist_item_id, local_date FROM checklist_tick "
            "WHERE local_date BETWEEN ? AND ?",
            (days[0].isoformat(), days[-1].isoformat()),
        )
    }
    rows = [
        GridRow(
            item_id=int(item["id"]),
            title=str(item["title"]),
            streak=checklist.streak(conn, settings, int(item["id"]), today),
            cells=[
                Cell(
                    day=day,
                    scheduled=checklist.scheduled_on(int(item["weekday_mask"]), day),
                    ticked=(int(item["id"]), day.isoformat()) in ticked,
                    is_today=day == today,
                    is_future=day > today,
                )
                for day in days
            ],
        )
        for item in items
    ]
    due_today = [
        r for r in rows for c in r.cells if c.is_today and c.scheduled
    ]
    return WeekGrid(
        days=days,
        today=today,
        rows=rows,
        done_today=sum(1 for r in due_today if any(c.is_today and c.ticked for c in r.cells)),
        total_today=len(due_today),
    )


HEAT_WEEKS = 8


@dataclass(frozen=True)
class HeatCell:
    day: date
    scheduled: int
    ticked: int
    future: bool

    @property
    def denominator(self) -> int:
        """A tick on an unscheduled day is real work, not an impossible claim:
        the denominator grows to meet it, so no cell ever reads over 100%."""
        return max(self.scheduled, self.ticked)

    @property
    def bucket(self) -> int:
        """0–4 fill step. §5 sequential magnitude: one ink, stepped opacity."""
        if not self.denominator or not self.ticked:
            return 0
        share = self.ticked / self.denominator
        if share >= 1:
            return 4
        if share >= 0.67:
            return 3
        if share >= 0.34:
            return 2
        return 1

    @property
    def label(self) -> str:
        return f"{self.ticked}/{self.denominator} · {self.day.strftime('%d %b')}"


@dataclass(frozen=True)
class Heatmap:
    weeks: list[list[HeatCell]]

    @property
    def empty(self) -> bool:
        return not self.weeks


def heat(conn: sqlite3.Connection, settings: Settings, today: date) -> Heatmap:
    """Consistency over the last HEAT_WEEKS weeks, one cell per day.

    Scheduled counts use today's active items and masks — the schedule has no
    history table, so a mask edited last week recolors the past. Stated
    approximation, same one the streak calculation already makes.
    """
    start = week_start_of(today, settings.week_start) - timedelta(weeks=HEAT_WEEKS - 1)
    items = conn.execute(
        "SELECT id, weekday_mask FROM checklist_item WHERE user_id = ? AND active = 1",
        (USER_ID,),
    ).fetchall()
    if not items:
        return Heatmap(weeks=[])
    end = start + timedelta(days=HEAT_WEEKS * 7 - 1)
    # Joined on active items: a deactivated item's history must not inflate a
    # day past what the denominator (active items only) can substantiate.
    ticked_by_day = {
        str(r["local_date"]): int(r["n"])
        for r in conn.execute(
            "SELECT t.local_date, COUNT(*) AS n FROM checklist_tick t "
            "JOIN checklist_item i ON i.id = t.checklist_item_id "
            "WHERE i.user_id = ? AND i.active = 1 "
            "AND t.local_date BETWEEN ? AND ? GROUP BY t.local_date",
            (USER_ID, start.isoformat(), end.isoformat()),
        )
    }
    weeks: list[list[HeatCell]] = []
    for w in range(HEAT_WEEKS):
        week: list[HeatCell] = []
        for i in range(7):
            day = start + timedelta(weeks=w, days=i)
            week.append(
                HeatCell(
                    day=day,
                    scheduled=sum(
                        1
                        for item in items
                        if checklist.scheduled_on(int(item["weekday_mask"]), day)
                    ),
                    ticked=ticked_by_day.get(day.isoformat(), 0),
                    future=day > today,
                )
            )
        weeks.append(week)
    return Heatmap(weeks=weeks)


@dataclass(frozen=True)
class GoalCard:
    goal_id: int
    title: str
    horizon: str
    target_date: str | None
    definition_of_done: str
    targets: list[dict[str, Any]]
    stale: Any
    risk: Any
    roadmap_id: int | None = None
    roadmap_title: str | None = None

    @property
    def cadence(self) -> list[dict[str, Any]]:
        return [t for t in self.targets if t["weekly_count"]]

    @property
    def milestones(self) -> list[dict[str, Any]]:
        """Format-audit ruling: milestones are dates, not streams. They render as
        a compact list, not full-height target rows whose body is "last —"."""
        return [t for t in self.targets if t["kind"] == "milestone"]

    @property
    def active_targets(self) -> list[dict[str, Any]]:
        return [t for t in self.targets if t["kind"] != "milestone"]

    @property
    def behind(self) -> list[dict[str, Any]]:
        return [t for t in self.cadence if t["done_this_week"] < t["weekly_count"]]

    @property
    def flagged(self) -> bool:
        """What puts a goal above the fold: it has gone quiet, its trajectory misses
        its target date, or a cadence is behind this week.

        G11 holds — the three are read independently and reported independently on the
        card. This is a routing decision about which of two lists the card lands in,
        not a merged health score, and nothing on the page renders it as a number.
        """
        return bool(
            (self.stale is not None and self.stale.level in ("warn", "serious"))
            or (self.risk is not None and self.risk.at_risk)
            or self.behind
        )

    @property
    def next_target(self) -> dict[str, Any] | None:
        """The cadence the collapsed row names: the first one behind, else the first."""
        if self.behind:
            return self.behind[0]
        return self.cadence[0] if self.cadence else None

    @property
    def week_done(self) -> int:
        return sum(int(t["done_this_week"]) for t in self.cadence)

    @property
    def week_needed(self) -> int:
        return sum(int(t["weekly_count"]) for t in self.cadence)

    @property
    def last_checkpoint(self) -> str | None:
        stamps = [str(t["last_checkpoint"]) for t in self.targets if t["last_checkpoint"]]
        return max(stamps) if stamps else None


@dataclass(frozen=True)
class Cards:
    cards: list[GoalCard]
    inert: list[dict[str, Any]]

    @property
    def empty(self) -> bool:
        return not self.cards and not self.inert

    @property
    def flagged(self) -> list[GoalCard]:
        """The worst two, not everyone with a blemish.

        Format-audit ruling: when every goal qualifies, a NEEDS ATTENTION section
        discriminates nothing. Ranked by severity — trajectory miss first (a G13
        at-risk is a claim about the target date), then staleness depth, then how
        many cadences are behind — and capped, so the section always answers
        "what do I look at first". The rest keep their chips under ON TRACK;
        G11 holds, nothing is merged into a score, this is a routing order.
        """
        flagged = [c for c in self.cards if c.flagged]

        def severity(c: GoalCard) -> tuple[int, int, int]:
            stale_rank = {"serious": 2, "warn": 1}.get(c.stale.level if c.stale else "", 0)
            return (
                int(c.risk is not None and c.risk.at_risk),
                stale_rank,
                len(c.behind),
            )

        flagged.sort(key=severity, reverse=True)  # stable: ties keep goal order
        return flagged[:2]

    @property
    def healthy(self) -> list[GoalCard]:
        above = {c.goal_id for c in self.flagged}
        return [c for c in self.cards if c.goal_id not in above]


def goal_cards(conn: sqlite3.Connection, settings: Settings, today: date) -> Cards:
    start = week_start_of(today, settings.week_start)
    rows = conn.execute(
        query("dashboard_goals"),
        {"user_id": USER_ID, "week_start": start.isoformat()},
    ).fetchall()
    staleness = {s.goal_id: s for s in health.staleness(conn, settings, today)}
    risks = {r.goal_id: r for r in health.risk(conn, settings, today)}
    # The clock a periodic target is read against is one rule (`TargetProgress.level`),
    # and the page reads it rather than restating it in SQL or in Jinja. A due date
    # computed in two places is a due date that disagrees with the brief.
    clocks = {
        p.target_id: p
        for p in targets_mod.progress(conn, settings, today)
        if p.kind == "periodic"
    }
    # The two pages cross-reference instead of merging: a roadmap-backed goal carries a
    # text link to its roadmap, where the step ledger and clinical context live.
    roadmaps = {
        int(r["goal_id"]): (int(r["id"]), str(r["title"]))
        for r in conn.execute(
            "SELECT id, goal_id, title FROM roadmap "
            "WHERE user_id = ? AND status = 'active' ORDER BY id",
            (USER_ID,),
        )
    }

    cards: list[GoalCard] = []
    inert: list[dict[str, Any]] = []
    for row in rows:
        if row["target_id"] is None:
            inert.append(dict(row))
            continue
        goal_id = int(row["goal_id"])
        if not cards or cards[-1].goal_id != goal_id:
            roadmap = roadmaps.get(goal_id)
            cards.append(
                GoalCard(
                    goal_id=goal_id,
                    title=str(row["goal_title"]),
                    horizon=str(row["horizon"]),
                    target_date=row["target_date"],
                    definition_of_done=str(row["definition_of_done"]),
                    targets=[],
                    stale=staleness.get(goal_id),
                    risk=risks.get(goal_id),
                    roadmap_id=roadmap[0] if roadmap else None,
                    roadmap_title=roadmap[1] if roadmap else None,
                )
            )
        target = dict(row)
        clock = clocks.get(int(row["target_id"]))
        if clock is not None:
            target["every_days"] = clock.every_days
            target["days_since"] = clock.days_since
            target["level"] = clock.level
            target["chip_text"] = clock.chip()
        cards[-1].targets.append(target)
    return Cards(cards=cards, inert=inert)


@dataclass(frozen=True)
class Pace:
    """What the THIS WEEK summary carries, so the fold states its own contents.

    All that survives of the retired three-reel strip. "Done today" and "best streak"
    were the banner's n/m and the grid's streak column read twice; this number is the
    only one the collapsed section could not otherwise show.
    """

    on_pace: int
    total: int


def pace(cards: Cards) -> Pace:
    cadence = [t for c in cards.cards for t in c.targets if t["weekly_count"]]
    return Pace(
        on_pace=sum(1 for t in cadence if t["done_this_week"] >= t["weekly_count"]),
        total=len(cadence),
    )


def build_router(
    templates: Jinja2Templates,
    settings: Settings,
    get_conn: Callable[[], Any],
    today: Callable[[], date],
) -> APIRouter:
    router = APIRouter()

    def ticks_fragment(request: Request, conn: sqlite3.Connection) -> Any:
        """A tick replaces #today-ticks (banner n/m included) and refreshes the week
        grid out of band. The THIS WEEK <details> wrapper sits above both in
        goals.html and is never in the response, so an open fold stays open."""
        day = today()
        return templates.TemplateResponse(
            request,
            "_today_ticks.html",
            {
                "grid": week_grid(conn, settings, day),
                "heat": heat(conn, settings, day),
                "today": day,
                "week_oob": True,
            },
        )

    def cards_fragment(request: Request, conn: sqlite3.Connection) -> Any:
        """A cadence write moves "targets on pace", which renders in the THIS WEEK
        summary — an OOB copy of that one span rides along with the cards swap."""
        day = today()
        cards = goal_cards(conn, settings, day)
        return templates.TemplateResponse(
            request,
            "_goal_cards.html",
            {
                "g": cards,
                "pace": pace(cards),
                "today": day,
                "pace_oob": True,
            },
        )

    @router.get("/goals", response_class=HTMLResponse)
    def goals(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
        day = today()
        grid = week_grid(conn, settings, day)
        cards = goal_cards(conn, settings, day)
        return templates.TemplateResponse(
            request,
            "goals.html",
            {
                "grid": grid,
                "g": cards,
                "heat": heat(conn, settings, day),
                "pace": pace(cards),
                "day": day,
                "today": day,
                "week_start": week_start_of(day, settings.week_start),
                "settings": settings,
            },
        )

    @router.post("/goals/checklist/{item_id}/{action}", response_class=HTMLResponse)
    def tick(
        item_id: RowId,
        action: str,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Same writes as the dashboard endpoints, different fragment back — the week
        grid re-renders, not the dashboard panel."""
        if action not in ("tick", "untick"):
            raise HTTPException(status_code=422, detail=f"unknown action {action!r}")
        fn = actions.tick if action == "tick" else actions.untick
        try:
            fn(conn, item_id, today().isoformat())
        except actions.ActionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ticks_fragment(request, conn)

    @router.post("/goals/targets/{target_id}/log", response_class=HTMLResponse)
    def log_total(
        target_id: RowId,
        request: Request,
        amount: int = Form(...),
        note: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """Log against a lifetime total from the Goals page — the roadmap page's
        endpoint scoped to any goal, so non-roadmap totals (a startup metric, a
        reading count) are loggable where they render."""
        if amount <= 0:
            raise HTTPException(status_code=422, detail="amount must be positive")
        row = conn.execute(
            "SELECT t.id FROM target t JOIN goal g ON g.id = t.goal_id "
            "WHERE t.id = ? AND g.user_id = ? AND t.kind = 'total' AND t.active = 1",
            (target_id, USER_ID),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404)
        # The funnel's own refusals are owner-readable ("record the sessions
        # separately"); without this they surfaced as a bare 500 and the generic strip
        # in base.html said only "The server refused that". The roadmap page's sibling
        # endpoint has always caught this — the two log forms should not disagree about
        # what a rejected amount looks like.
        try:
            checkpoints.record(
                conn, target_id, source="manual",
                occurred_at=timezones.local_now_iso(settings, today()),
                note=note.strip() or None, delta=amount,
            )
        except checkpoints.CheckpointError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return cards_fragment(request, conn)

    @router.post("/goals/targets/{target_id}/tick", response_class=HTMLResponse)
    def tick_cadence(
        target_id: RowId,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """+1 on a cadence target — the app's missing manual write.

        G3 is the whole design of this endpoint: it records a checkpoint and never
        touches progress, which stays a sum over checkpoints. It is deliberately NOT
        idempotent, and that is not a violation of the idempotency rule: that rule
        governs syncs, which re-read unchanged upstream state. Two presses of +1 are
        two sessions done, so they are two checkpoints — an idempotent +1 would make
        the second real session unrecordable. Removal is `checkpoints.delete`.
        """
        row = conn.execute(
            "SELECT t.id FROM target t JOIN goal g ON g.id = t.goal_id "
            "WHERE t.id = ? AND g.user_id = ? AND t.kind = 'cadence' AND t.active = 1",
            (target_id, USER_ID),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404)
        checkpoints.record(
            conn, target_id, source="manual",
            occurred_at=timezones.local_now_iso(settings, today()),
        )
        return cards_fragment(request, conn)

    @router.post("/goals/targets/{target_id}/weekly/{count}", response_class=HTMLResponse)
    def weekly(
        target_id: RowId,
        count: int,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        try:
            actions.set_weekly_count(conn, target_id, count)
        except actions.ActionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return cards_fragment(request, conn)

    return router
