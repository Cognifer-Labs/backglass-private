"""The command palette's three routes: list, run, read.

Deliberately three small ones rather than a page. The palette is an overlay on whatever
the owner is already looking at — a command is something you reach for mid-task, and
navigating away from the thing that made you reach for it is the wrong shape.

The run route takes a registry *key*, never a command string. That is the whole security
story and it is worth stating plainly: there is no path from this router to a shell, an
argument list, or an import name. A key that is not in `commands.BY_KEY` produces a 404
and nothing else.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from backglass.config import Settings
from backglass.web import commands as commands_mod
from backglass.web.jobs import Runner


def build_router(
    templates: Jinja2Templates,
    settings: Settings,
    get_conn: Callable[[], Any],
    today: Callable[[], date],
    runner: Runner | None = None,
) -> APIRouter:
    del get_conn, today  # every command opens its own connection on its own thread
    router = APIRouter()
    jobs = runner or Runner()

    def _panel(request: Request, refusal: str | None = None) -> Any:
        return templates.TemplateResponse(
            request,
            "_command_run.html",
            {"job": jobs.current, "refusal": refusal},
        )

    @router.get("/commands", response_class=HTMLResponse)
    def listing(request: Request) -> Any:
        """The palette's contents. Rendered server-side rather than shipped as JSON and
        templated in the browser, because there is no build step here (docs/10) and the
        list is nine rows that change when the registry does."""
        return templates.TemplateResponse(
            request,
            "_command_list.html",
            {"commands": commands_mod.as_rows(), "job": jobs.current},
        )

    @router.post("/commands/{key}/run", response_class=HTMLResponse)
    def run(key: str, request: Request) -> Any:
        command = commands_mod.get(key)
        if command is None:
            # 404 rather than a message: an unknown key is not a user mistake the palette
            # can help with, it is a request for something that does not exist.
            raise HTTPException(status_code=404, detail="no such command")
        _, refusal = jobs.start(command, settings)
        return _panel(request, refusal)

    @router.get("/commands/running", response_class=HTMLResponse)
    def running(request: Request) -> Any:
        """What the current job is doing. HTMX polls this while one is live and stops
        when the fragment comes back without a polling trigger — so a finished job costs
        no further requests, and a page left open overnight is not a request per second
        for twelve hours."""
        return _panel(request)

    return router
