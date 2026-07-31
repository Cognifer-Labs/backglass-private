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

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from backglass.config import Settings
from backglass.goals import activities, checkpoints, health
from backglass.ledger import USER_ID
from backglass.plan import timezones
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


def totals_for(conn: sqlite3.Connection, goal_id: int) -> list[dict[str, Any]]:
    """The goal's lifetime accumulators, each with its last three log entries —
    the number and its provenance travel together (rule 4)."""
    rows = conn.execute(
        "SELECT t.id, t.title, t.total_count, "
        "  COALESCE((SELECT SUM(c.delta) FROM checkpoint c WHERE c.target_id = t.id), 0)"
        "  AS done "
        "FROM target t WHERE t.goal_id = ? AND t.kind = 'total' AND t.active = 1 "
        "ORDER BY t.id",
        (goal_id,),
    ).fetchall()
    totals: list[dict[str, Any]] = []
    for row in rows:
        entries = conn.execute(
            "SELECT c.occurred_at, c.delta, c.note, a.title AS activity "
            "FROM checkpoint c LEFT JOIN activity a ON a.id = c.activity_id "
            "WHERE c.target_id = ? ORDER BY c.occurred_at DESC, c.id DESC LIMIT 3",
            (row["id"],),
        ).fetchall()
        totals.append({**dict(row), "entries": entries})
    return totals


def progress_context(
    conn: sqlite3.Connection,
    settings: Settings,
    day: date,
    detail: dict[str, Any],
) -> dict[str, Any]:
    """Everything the roadmap page claims about progress, derived on read:
    step counts, the next pending step, and the goal's staleness + risk (G11:
    two signals, never merged)."""
    goal_id = int(detail["r"]["goal_id"])
    steps = detail["steps"]
    detail["done_steps"] = sum(1 for s in steps if s["status"] == "done")
    detail["live_steps"] = sum(1 for s in steps if s["status"] != "skipped")
    detail["next_step"] = next((s for s in steps if s["status"] == "pending"), None)
    detail["stale"] = next(
        (s for s in health.staleness(conn, settings, day) if s.goal_id == goal_id), None
    )
    detail["risk"] = next(
        (r for r in health.risk(conn, settings, day) if r.goal_id == goal_id), None
    )
    detail["totals"] = totals_for(conn, goal_id)
    detail["activities"] = activities.list_with_hours(conn)
    detail["categories"] = activities.CATEGORIES
    detail["amcas_slots"] = activities.AMCAS_SLOTS
    return detail


def build_router(
    templates: Jinja2Templates,
    settings: Settings,
    get_conn: Callable[[], Any],
    today: Callable[[], date],
) -> APIRouter:
    router = APIRouter()

    def _detail(conn: sqlite3.Connection, roadmap_id: int) -> dict[str, Any]:
        detail = roadmap_detail(conn, roadmap_id)
        if detail is None:
            raise HTTPException(status_code=404)
        detail["settings"] = settings
        return progress_context(conn, settings, today(), detail)

    def steps_fragment(request: Request, conn: sqlite3.Connection, roadmap_id: int) -> Any:
        return templates.TemplateResponse(
            request, "_roadmap_steps.html", _detail(conn, roadmap_id)
        )

    def totals_fragment(request: Request, conn: sqlite3.Connection, roadmap_id: int) -> Any:
        return templates.TemplateResponse(
            request, "_roadmap_totals.html", _detail(conn, roadmap_id)
        )

    def _total_target(
        conn: sqlite3.Connection, roadmap_id: int, target_id: int
    ) -> dict[str, Any]:
        """A totals write must land on a total target of this roadmap's goal —
        anything else is a 404, not a silent write to someone else's number."""
        row = conn.execute(
            "SELECT t.id FROM target t JOIN roadmap r ON r.goal_id = t.goal_id "
            "WHERE r.id = ? AND t.id = ? AND t.kind = 'total' AND t.active = 1",
            (roadmap_id, target_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404)
        return dict(row)

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
        return templates.TemplateResponse(request, "roadmap.html", _detail(conn, roadmap_id))

    @router.post(
        "/roadmaps/{roadmap_id}/totals/{target_id}/log", response_class=HTMLResponse
    )
    def total_log(
        roadmap_id: int,
        target_id: int,
        request: Request,
        amount: int = Form(...),
        note: str = Form(""),
        activity_id: int = Form(0),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """One log entry = one checkpoint: delta carries the amount, the note carries
        the org/supervisor detail AMCAS will ask for later (G9). Naming an activity
        (docs/14 F1) files the same hours under the discrete extracurricular the
        Work & Activities export will assemble from."""
        if amount <= 0:
            raise HTTPException(status_code=422, detail="amount must be positive")
        _total_target(conn, roadmap_id, target_id)
        try:
            checkpoints.record(
                conn, target_id, source="manual",
                occurred_at=timezones.local_now_iso(settings, today()),
                note=note.strip() or None, delta=amount,
                activity_id=activity_id or None,
            )
        except checkpoints.CheckpointError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return totals_fragment(request, conn, roadmap_id)

    @router.post("/roadmaps/{roadmap_id}/activities", response_class=HTMLResponse)
    def activity_add(
        roadmap_id: int,
        request: Request,
        title: str = Form(...),
        org: str = Form(""),
        role: str = Form(""),
        category: str = Form("other"),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        _detail(conn, roadmap_id)  # 404 before any write
        try:
            activities.add(conn, title=title, org=org, role=role, category=category)
        except activities.ActivityError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return totals_fragment(request, conn, roadmap_id)

    @router.post(
        "/roadmaps/{roadmap_id}/activities/{activity_id}/meaningful",
        response_class=HTMLResponse,
    )
    def activity_meaningful(
        roadmap_id: int,
        activity_id: int,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """AMCAS allows three "most meaningful" — surfaced as a count, never enforced."""
        _detail(conn, roadmap_id)
        try:
            activities.toggle_most_meaningful(conn, activity_id)
        except activities.ActivityError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return totals_fragment(request, conn, roadmap_id)

    @router.post(
        "/roadmaps/{roadmap_id}/totals/{target_id}/set", response_class=HTMLResponse
    )
    def total_set(
        roadmap_id: int,
        target_id: int,
        request: Request,
        total: int = Form(...),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """G4's spirit: changing the bar you are measured against is a legitimate
        edit, not cheating — logged hours stay untouched."""
        if total <= 0:
            raise HTTPException(status_code=422, detail="total must be positive")
        _total_target(conn, roadmap_id, target_id)
        conn.execute("UPDATE target SET total_count = ? WHERE id = ?", (total, target_id))
        return totals_fragment(request, conn, roadmap_id)

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
