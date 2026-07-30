"""The Roadmaps page: preset career paths instantiated into the goal engine. Phase 6.

Reads join roadmap_step to target so the step list and the goal engine can never
disagree about what is active. Step edits return the steps fragment; drop is the
third destructive action in the app and gets a confirmation like the others.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from backglass.config import Settings
from backglass.ledger import USER_ID
from backglass.roadmap import adjust, instantiate, presets


def list_roadmaps(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT r.*, g.target_date, g.status AS goal_status, "
        "  (SELECT COUNT(*) FROM roadmap_step s WHERE s.roadmap_id = r.id "
        "   AND s.status = 'done') AS done_steps, "
        "  (SELECT COUNT(*) FROM roadmap_step s WHERE s.roadmap_id = r.id "
        "   AND s.status != 'skipped') AS live_steps "
        "FROM roadmap r JOIN goal g ON g.id = r.goal_id "
        "WHERE r.user_id = ? ORDER BY r.status = 'active' DESC, r.id",
        (USER_ID,),
    ).fetchall()


def roadmap_detail(conn: sqlite3.Connection, roadmap_id: int) -> dict[str, Any] | None:
    head = conn.execute(
        "SELECT r.*, g.target_date, g.definition_of_done FROM roadmap r "
        "JOIN goal g ON g.id = r.goal_id WHERE r.id = ? AND r.user_id = ?",
        (roadmap_id, USER_ID),
    ).fetchone()
    if head is None:
        return None
    steps = conn.execute(
        "SELECT s.*, t.active AS target_active FROM roadmap_step s "
        "LEFT JOIN target t ON t.id = s.target_id "
        "WHERE s.roadmap_id = ? ORDER BY s.sort_order",
        (roadmap_id,),
    ).fetchall()
    cadences = conn.execute(
        "SELECT c.cadence_key, t.title, t.weekly_count, t.estimated_minutes_each "
        "FROM roadmap_cadence c JOIN target t ON t.id = c.target_id "
        "WHERE c.roadmap_id = ?",
        (roadmap_id,),
    ).fetchall()
    return {"r": dict(head), "steps": steps, "cadences": cadences}


def build_router(
    templates: Jinja2Templates,
    settings: Settings,
    get_conn: Callable[[], Any],
    today: Callable[[], date],
) -> APIRouter:
    router = APIRouter()

    def steps_fragment(request: Request, conn: sqlite3.Connection, roadmap_id: int) -> Any:
        detail = roadmap_detail(conn, roadmap_id)
        if detail is None:
            raise HTTPException(status_code=404)
        detail["settings"] = settings
        return templates.TemplateResponse(request, "_roadmap_steps.html", detail)

    @router.get("/roadmaps", response_class=HTMLResponse)
    def roadmaps(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
        return templates.TemplateResponse(
            request,
            "roadmaps.html",
            {
                "rows": list_roadmaps(conn),
                "paths": presets.list_paths(),
                "settings": settings,
            },
        )

    @router.get("/roadmaps/{roadmap_id}", response_class=HTMLResponse)
    def roadmap_page(
        roadmap_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Any:
        detail = roadmap_detail(conn, roadmap_id)
        if detail is None:
            raise HTTPException(status_code=404)
        detail["settings"] = settings
        return templates.TemplateResponse(request, "roadmap.html", detail)

    @router.post("/roadmaps/start/{path_id}", response_class=HTMLResponse)
    def start(path_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
        """Un-personalized instantiation. The AI interview is CLI-only — it needs a
        terminal conversation, and pretending otherwise in a form would be worse."""
        try:
            preset = presets.load(path_id)
        except presets.PresetError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        roadmap_id = instantiate.instantiate(conn, settings, preset, today())
        return RedirectResponse(url=f"/roadmaps/{roadmap_id}", status_code=303)

    def _adjust(fn: Callable[..., Any], *args: Any) -> None:
        try:
            fn(*args)
        except adjust.AdjustError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/roadmaps/{roadmap_id}/steps/{step_id}/done", response_class=HTMLResponse)
    def step_done(
        roadmap_id: int, step_id: int, request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        _adjust(adjust.complete_step, conn, step_id)
        return steps_fragment(request, conn, roadmap_id)

    @router.post("/roadmaps/{roadmap_id}/steps/{step_id}/skip", response_class=HTMLResponse)
    def step_skip(
        roadmap_id: int, step_id: int, request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        _adjust(adjust.skip_step, conn, step_id)
        return steps_fragment(request, conn, roadmap_id)

    @router.post("/roadmaps/{roadmap_id}/steps/{step_id}/unskip", response_class=HTMLResponse)
    def step_unskip(
        roadmap_id: int, step_id: int, request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        _adjust(adjust.unskip_step, conn, step_id)
        return steps_fragment(request, conn, roadmap_id)

    @router.post(
        "/roadmaps/{roadmap_id}/steps/{step_id}/date/{iso}", response_class=HTMLResponse
    )
    def step_date(
        roadmap_id: int, step_id: int, iso: str, request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        try:
            new_date = date.fromisoformat(iso)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"bad date {iso!r}") from exc
        _adjust(adjust.redate_step, conn, step_id, new_date)
        return steps_fragment(request, conn, roadmap_id)

    @router.post(
        "/roadmaps/{roadmap_id}/steps/{step_id}/move/{direction}", response_class=HTMLResponse
    )
    def step_move(
        roadmap_id: int, step_id: int, direction: str, request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        if direction not in ("up", "down"):
            raise HTTPException(status_code=422, detail=f"bad direction {direction!r}")
        _adjust(adjust.move_step, conn, step_id, direction)
        return steps_fragment(request, conn, roadmap_id)

    @router.post("/roadmaps/{roadmap_id}/drop", response_class=HTMLResponse)
    def drop(roadmap_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
        _adjust(adjust.drop_roadmap, conn, roadmap_id)
        return RedirectResponse(url="/roadmaps", status_code=303)

    return router
