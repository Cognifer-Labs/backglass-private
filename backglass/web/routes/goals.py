"""The Goals page: goal cards with their targets, and the checklist as a week grid.

docs/04 §2. The dashboard panels stay the at-a-glance summary; this page is the deep
view. Two deliberate framings from the spec shape it:

- Goals are cards grouped by goal, not a flat target list, because staleness and risk
  are goal-level signals (G11–G13) and the definition of done belongs next to the
  progress it defines ("a goal without one is a mood").
- The checklist renders as the current week, not just today, because a habit is a
  row over days. Streaks show as a bare count (C4: quiet), and the only interactive
  cell is today — the past is a record, not an editing surface.
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
from backglass.ledger import USER_ID
from backglass.plan import timezones
from backglass.web import actions
from backglass.web.panels import week_start_of


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


@dataclass(frozen=True)
class Cards:
    cards: list[GoalCard]
    inert: list[dict[str, Any]]

    @property
    def empty(self) -> bool:
        return not self.cards and not self.inert


def goal_cards(conn: sqlite3.Connection, settings: Settings, today: date) -> Cards:
    start = week_start_of(today, settings.week_start)
    rows = conn.execute(
        query("dashboard_goals"),
        {"user_id": USER_ID, "week_start": start.isoformat()},
    ).fetchall()
    staleness = {s.goal_id: s for s in health.staleness(conn, settings, today)}
    risks = {r.goal_id: r for r in health.risk(conn, settings, today)}

    cards: list[GoalCard] = []
    inert: list[dict[str, Any]] = []
    for row in rows:
        if row["target_id"] is None:
            inert.append(dict(row))
            continue
        goal_id = int(row["goal_id"])
        if not cards or cards[-1].goal_id != goal_id:
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
                )
            )
        cards[-1].targets.append(dict(row))
    return Cards(cards=cards, inert=inert)


@dataclass(frozen=True)
class Kpis:
    """The page's three reel readouts — exactly three, §7's per-view budget."""

    done_today: int
    total_today: int
    best_streak: int
    on_pace: int
    cadence_total: int


def kpis(grid: WeekGrid, cards: Cards) -> Kpis:
    cadence = [t for c in cards.cards for t in c.targets if t["weekly_count"]]
    return Kpis(
        done_today=grid.done_today,
        total_today=grid.total_today,
        best_streak=max((r.streak for r in grid.rows), default=0),
        on_pace=sum(1 for t in cadence if t["done_this_week"] >= t["weekly_count"]),
        cadence_total=len(cadence),
    )


def build_router(
    templates: Jinja2Templates,
    settings: Settings,
    get_conn: Callable[[], Any],
    today: Callable[[], date],
) -> APIRouter:
    router = APIRouter()

    def grid_fragment(request: Request, conn: sqlite3.Connection) -> Any:
        """The tick swap re-renders the whole checklist section — KPI strip and
        heatmap live inside it, so both stay truthful without OOB machinery."""
        day = today()
        grid = week_grid(conn, settings, day)
        return templates.TemplateResponse(
            request,
            "_check_week.html",
            {
                "grid": grid,
                "heat": heat(conn, settings, day),
                "k": kpis(grid, goal_cards(conn, settings, day)),
            },
        )

    def cards_fragment(request: Request, conn: sqlite3.Connection) -> Any:
        """A cadence change moves the on-pace KPI, which lives in the checklist
        section — an OOB copy of the strip rides along with the cards swap."""
        day = today()
        cards = goal_cards(conn, settings, day)
        return templates.TemplateResponse(
            request,
            "_goal_cards.html",
            {
                "g": cards,
                "k": kpis(week_grid(conn, settings, day), cards),
                "kpi_oob": True,
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
                "k": kpis(grid, cards),
                "day": day,
                "week_start": week_start_of(day, settings.week_start),
                "settings": settings,
            },
        )

    @router.post("/goals/checklist/{item_id}/{action}", response_class=HTMLResponse)
    def tick(
        item_id: int,
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
        return grid_fragment(request, conn)

    @router.post("/goals/targets/{target_id}/log", response_class=HTMLResponse)
    def log_total(
        target_id: int,
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
        checkpoints.record(
            conn, target_id, source="manual",
            occurred_at=timezones.local_now_iso(settings, today()),
            note=note.strip() or None, delta=amount,
        )
        return cards_fragment(request, conn)

    @router.post("/goals/targets/{target_id}/weekly/{count}", response_class=HTMLResponse)
    def weekly(
        target_id: int,
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
