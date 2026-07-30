"""The dashboard. One page, server-rendered, responsive. docs/06.

FastAPI + Jinja2 + HTMX, no build step, no npm, no bundler (docs/10 §Web layer). HTMX is
the load-bearing choice: every write-back endpoint returns the re-rendered fragment and
HTMX swaps it in place, so there is no client state and no API to keep in sync with a
frontend. For a seven-action dashboard used by one person, that is the entire
justification for not writing a SPA.

Read paths are in `panels.py`, writes in `actions.py`. This module is routing and nothing
else.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from backglass.config import REPO_ROOT, Settings, get_settings
from backglass.db import connect, migrate
from backglass.web import actions, panels

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
# docs/06 §The Sources panel wants a "relative timestamp of last successful sync". The
# formatting lives in panels.py so it is testable without rendering a page.
templates.env.filters["relative"] = panels.relative
templates.env.filters["due"] = panels.due_label

#: A 1x1 transparent GIF, for docs/05 B7. Inlined rather than shipped as a file because
#: a 43-byte asset with its own path is a thing that can go missing in a deploy.
PIXEL = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c000000000100010000020144003b"
)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    app = FastAPI(title="Backglass", docs_url=None, redoc_url=None, openapi_url=None)

    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    # docs/10 §Web layer: "CSS is design/tokens.css plus hand-written rules." Served from
    # design/ so the tokens the dashboard uses are literally the ones
    # scripts/validate-palette.mjs checks, not a copy that can drift from them.
    app.mount("/design", StaticFiles(directory=str(REPO_ROOT / "design")), name="design")

    def get_conn() -> Any:
        conn = connect(resolved.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def today() -> date:
        from backglass.brief.daily import today_in

        return today_in(resolved.default_tz)

    # ── sidebar state (Phase 6 rework) ────────────────────────────────────
    # Computed once per full-page GET and stashed on request.state, where base.html
    # reads it. Fragments, POSTs and asset mounts skip the work: an HTMX swap replaces
    # a panel, not the shell, so recomputing sidebar state for it would be waste.

    @app.middleware("http")
    async def sidebar_state(request: Request, call_next: Any) -> Any:
        request.state.sb = None
        wants_shell = (
            request.method == "GET"
            and "hx-request" not in request.headers
            and not request.url.path.startswith(("/static", "/design", "/b/"))
        )
        if wants_shell:
            conn = connect(resolved.db_path)
            try:
                request.state.sb = panels.sidebar(conn, resolved, today())
            finally:
                conn.close()
        return await call_next(request)

    # ── per-page routers (Phase 6) ────────────────────────────────────────

    from backglass.web.routes import goals as goals_routes
    from backglass.web.routes import people as people_routes
    from backglass.web.routes import roadmaps as roadmap_routes
    from backglass.web.routes import schedule as schedule_routes

    app.include_router(schedule_routes.build_router(templates, resolved, get_conn, today))
    app.include_router(goals_routes.build_router(templates, resolved, get_conn, today))
    app.include_router(people_routes.build_router(templates, resolved, get_conn, today))
    app.include_router(roadmap_routes.build_router(templates, resolved, get_conn, today))

    # ── read ──────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {"d": panels.everything(conn, resolved, today()), "settings": resolved},
        )

    @app.get("/b/{brief_id}.gif")
    def tracking_pixel(brief_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        """docs/05 B7. The brief was opened; that is the metric that matters most."""
        actions.mark_brief_opened(conn, brief_id)
        return Response(
            content=PIXEL,
            media_type="image/gif",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
            },
        )

    # ── fragments, returned by every write-back ───────────────────────────

    def board_fragment(request: Request, conn: sqlite3.Connection) -> Any:
        return templates.TemplateResponse(
            request, "_board.html", {"d": panels.everything(conn, resolved, today())}
        )

    def review_fragment(request: Request, conn: sqlite3.Connection) -> Any:
        return templates.TemplateResponse(
            request, "_review.html", {"d": panels.everything(conn, resolved, today())}
        )

    def checklist_fragment(request: Request, conn: sqlite3.Connection) -> Any:
        return templates.TemplateResponse(
            request, "_checklist.html", {"d": panels.everything(conn, resolved, today())}
        )

    def today_fragment(request: Request, conn: sqlite3.Connection) -> Any:
        return templates.TemplateResponse(
            request, "_today.html", {"d": panels.everything(conn, resolved, today())}
        )

    def goals_fragment(request: Request, conn: sqlite3.Connection) -> Any:
        return templates.TemplateResponse(
            request, "_goals.html", {"d": panels.everything(conn, resolved, today())}
        )

    def sources_fragment(request: Request, conn: sqlite3.Connection) -> Any:
        return templates.TemplateResponse(
            request, "_sources.html", {"d": panels.everything(conn, resolved, today())}
        )

    @app.post("/sources/{source}/{action}", response_class=HTMLResponse)
    def toggle_source(
        source: str,
        action: str,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """The pause switch. Disable keeps credential, cursor, and every stored item —
        the sync just skips the source until Resume."""
        from backglass.connectors import credentials

        if action not in ("enable", "disable"):
            raise HTTPException(status_code=422, detail=f"unknown action {action!r}")
        credentials.set_enabled(conn, source, action == "enable")
        return sources_fragment(request, conn)

    def _run(fn: Any, *args: Any, **kwargs: Any) -> None:
        try:
            fn(*args, **kwargs)
        except actions.ActionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ── write-back: the seven actions in docs/06 ──────────────────────────

    @app.post("/commitments/{commitment_id}/resolve", response_class=HTMLResponse)
    def resolve(
        commitment_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Any:
        _run(actions.resolve, conn, commitment_id)
        return board_fragment(request, conn)

    @app.post("/commitments/{commitment_id}/drop", response_class=HTMLResponse)
    def drop(
        commitment_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Any:
        _run(actions.drop, conn, commitment_id)
        return board_fragment(request, conn)

    @app.post("/commitments/{commitment_id}/snooze/{days}", response_class=HTMLResponse)
    def snooze(
        commitment_id: int,
        days: int,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        _run(actions.snooze, conn, commitment_id, days)
        return board_fragment(request, conn)

    @app.post("/review/{commitment_id}/accept", response_class=HTMLResponse)
    def accept(
        commitment_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Any:
        _run(actions.accept, conn, commitment_id)
        return review_fragment(request, conn)

    @app.post("/review/{commitment_id}/reject/{reason}", response_class=HTMLResponse)
    def reject(
        commitment_id: int,
        reason: str,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        _run(actions.reject, conn, commitment_id, reason)
        return review_fragment(request, conn)

    @app.post("/checklist/{item_id}/tick", response_class=HTMLResponse)
    def tick(
        item_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Any:
        _run(actions.tick, conn, item_id, today().isoformat())
        return checklist_fragment(request, conn)

    @app.post("/checklist/{item_id}/untick", response_class=HTMLResponse)
    def untick(
        item_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Any:
        _run(actions.untick, conn, item_id, today().isoformat())
        return checklist_fragment(request, conn)

    @app.post("/blocks/{block_id}/outcome/{outcome}", response_class=HTMLResponse)
    def block_outcome(
        block_id: int,
        outcome: str,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        _run(actions.set_block_outcome, conn, block_id, outcome)
        return today_fragment(request, conn)

    @app.post("/blocks/{block_id}/pin/{pinned}", response_class=HTMLResponse)
    def block_pin(
        block_id: int,
        pinned: int,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        _run(actions.pin_block, conn, block_id, bool(pinned))
        return today_fragment(request, conn)

    @app.post("/commitments/{commitment_id}/estimate/{minutes}", response_class=HTMLResponse)
    def estimate(
        commitment_id: int,
        minutes: int,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        _run(actions.set_estimate, conn, commitment_id, minutes)
        return board_fragment(request, conn)

    @app.post("/commitments/quick-add", response_class=HTMLResponse)
    def quick_add(
        request: Request,
        what: str = Form(...),
        direction: str = Form("i_owe"),
        counterparty: str = Form(""),
        due_at: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        _run(
            actions.quick_add,
            conn,
            resolved,
            what=what,
            direction=direction,
            counterparty=counterparty or None,
            due_at=due_at or None,
        )
        return board_fragment(request, conn)

    @app.post("/targets/{target_id}/weekly/{count}", response_class=HTMLResponse)
    def weekly(
        target_id: int,
        count: int,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        _run(actions.set_weekly_count, conn, target_id, count)
        return goals_fragment(request, conn)

    return app


def serve(
    settings: Settings | None = None, *, host: str = "127.0.0.1", port: int = 8765
) -> None:
    import uvicorn

    resolved = settings or get_settings()
    conn = connect(resolved.db_path)
    migrate(conn)
    conn.close()
    uvicorn.run(create_app(resolved), host=host, port=port, log_level="warning")
