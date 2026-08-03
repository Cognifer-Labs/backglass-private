"""The People page: searchable profiles over the entity table. Phase 6.

Search is a GET with HTMX swapping the rows fragment; every write-back returns a
fragment, same as the dashboard. Merge is the second destructive action in the app
(after drop) and gets the same confirmation treatment.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from backglass.config import Settings
from backglass.people import merge as merge_mod
from backglass.people import profiles, touch
from backglass.web import actions


def build_router(
    templates: Jinja2Templates,
    settings: Settings,
    get_conn: Callable[[], Any],
    today: Callable[[], date],
) -> APIRouter:
    router = APIRouter()

    def rows_context(
        conn: sqlite3.Connection, q: str | None, tag: str | None, cold: bool
    ) -> dict[str, Any]:
        rows = profiles.search(conn, q=q, tag=tag)
        touches = {t.entity_id: t for t in touch.cold(conn, settings, today())}
        if cold:
            rows = [
                r for r in rows
                if touches.get(r["id"]) and touches[r["id"]].level in ("warn", "cold")
            ]

        # Format-audit ruling: rank by what needs the owner (open commitments,
        # then how long quiet), and split people from service desks so the page
        # called People reads as one. Display grouping only — see profiles.org_like.
        def rank(r: dict[str, Any]) -> tuple[int, int, str]:
            t = touches.get(r["id"])
            days = t.days_since if t and t.days_since is not None else -1
            return (-int(r["open_count"] or 0), -days, str(r["canonical_name"]).lower())

        rows.sort(key=rank)
        persons = [r for r in rows if not profiles.org_like(r)]
        orgs = [r for r in rows if profiles.org_like(r)]
        return {"rows": rows, "persons": persons, "orgs": orgs, "touches": touches,
                "q": q or "", "tag": tag or "", "cold": cold}

    @router.get("/people", response_class=HTMLResponse)
    def people(
        request: Request,
        q: str | None = None,
        tag: str | None = None,
        cold: bool = Query(False),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        context = rows_context(conn, q, tag, cold)
        context["settings"] = settings
        # HTMX search-as-you-type swaps only the rows.
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(request, "_people_rows.html", context)
        return templates.TemplateResponse(request, "people.html", context)

    @router.get("/people/{entity_id}", response_class=HTMLResponse)
    def person(
        entity_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Any:
        record = profiles.profile(conn, entity_id)
        if record is None:
            raise HTTPException(status_code=404)
        touches = {t.entity_id: t for t in touch.cold(conn, settings, today())}
        return templates.TemplateResponse(
            request,
            "person.html",
            {
                "p": record,
                "touch": touches.get(entity_id),
                "timeline": profiles.timeline(conn, entity_id),
                "commitments": profiles.open_commitments(conn, entity_id),
                "derived": profiles.derived(conn, entity_id, today().isoformat()),
                "today": today(),
                "settings": settings,
            },
        )

    @router.post("/people", response_class=HTMLResponse)
    def create(
        request: Request,
        name: str = Form(...),
        role: str = Form(""),
        org: str = Form(""),
        tags: str = Form(""),
        notes: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        try:
            actions.person_create(
                conn, name=name, role=role or None, org=org or None, tags=tags,
                notes=notes or None,
            )
        except actions.ActionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        context = rows_context(conn, None, None, False)
        context["settings"] = settings
        return templates.TemplateResponse(request, "_people_rows.html", context)

    @router.post("/people/{entity_id}/edit", response_class=HTMLResponse)
    def edit(
        entity_id: int,
        role: str = Form(""),
        org: str = Form(""),
        tags: str = Form(""),
        notes: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        try:
            actions.person_update(
                conn, entity_id, role=role or None, org=org or None, tags=tags,
                notes=notes or None,
            )
        except actions.ActionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # Full-page action (the profile header changes shape); redirect, not fragment.
        return RedirectResponse(url=f"/people/{entity_id}", status_code=303)

    @router.post("/people/{winner_id}/merge/{loser_id}", response_class=HTMLResponse)
    def do_merge(
        winner_id: int,
        loser_id: int,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        try:
            merge_mod.merge(conn, winner_id, loser_id)
        except merge_mod.MergeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except sqlite3.Error as exc:
            # A merge repoints every table that names the loser and then deletes it, so
            # a table added later and not repointed fails the foreign key here. That is
            # a bug in merge(), but it must not reach the owner as a 500 with a
            # traceback: merge() rolls its transaction back, so nothing is half-applied
            # and the honest answer is that the merge did not happen and why.
            raise HTTPException(
                status_code=500,
                detail=f"merge failed and was rolled back; both people are unchanged: {exc}",
            ) from exc
        return RedirectResponse(url=f"/people/{winner_id}", status_code=303)

    return router
