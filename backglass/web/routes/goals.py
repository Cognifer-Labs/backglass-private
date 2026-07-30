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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from backglass.config import Settings
from backglass.db import query
from backglass.goals import checklist, health
from backglass.ledger import USER_ID
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


def build_router(
    templates: Jinja2Templates,
    settings: Settings,
    get_conn: Callable[[], Any],
    today: Callable[[], date],
) -> APIRouter:
    router = APIRouter()

    def grid_fragment(request: Request, conn: sqlite3.Connection) -> Any:
        return templates.TemplateResponse(
            request, "_check_week.html", {"grid": week_grid(conn, settings, today())}
        )

    def cards_fragment(request: Request, conn: sqlite3.Connection) -> Any:
        return templates.TemplateResponse(
            request, "_goal_cards.html", {"g": goal_cards(conn, settings, today())}
        )

    @router.get("/goals", response_class=HTMLResponse)
    def goals(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
        day = today()
        return templates.TemplateResponse(
            request,
            "goals.html",
            {
                "grid": week_grid(conn, settings, day),
                "g": goal_cards(conn, settings, day),
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
