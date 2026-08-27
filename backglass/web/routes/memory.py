"""The Memory page: the personal knowledge base, grouped by subject. Phase 12.

Read plus two writes (add, retract). Supersession happens in facts.remember, so the
page never edits a row in place — updating a fact is adding its replacement.

Since 2026-08-24 the page also carries the state doc (`backglass/situation.py`): the
same facts as a document, plus what recently changed, what the week holds and what the
open board is resting on. It belongs here rather than on a page of its own because it is
a rendering of these rows — reading it beside them is how a wrong line gets traced to the
fact behind it.

**The page renders the doc; it never stores a version.** Opening a page must not write,
and a version per page view would make the history a log of how often the owner looked at
it rather than a record of what moved. Versions come from the sync epilogue and from
`backglass situation --refresh`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from backglass import facts, situation, vault
from backglass.config import Settings
from backglass.web.params import RowId

#: How many versions of the state doc the page lists. The document's history is its
#: evolution and the owner wants to see it move; twenty entries is a season of change on a
#: ledger that only writes a version when something actually differs.
VERSION_LIMIT = 20


def build_router(
    templates: Jinja2Templates,
    settings: Settings,
    get_conn: Callable[[], Any],
    today: Callable[[], date],
) -> APIRouter:
    router = APIRouter()

    def state_doc(conn: sqlite3.Connection) -> dict[str, Any]:
        """The doc as it reads right now, and the versions behind it.

        Best-effort as a whole (rule 5): the knowledge base is the page's job and a state
        doc that cannot render must not take the facts down with it. `stored` is what the
        last stored version says, so the page can tell the owner when what they are
        reading has not been recorded yet — which is the honest state between a fact
        changing and the next sync.
        """
        try:
            body = situation.render(conn, settings, today())
            history = situation.versions(conn, limit=VERSION_LIMIT)
        except Exception:  # noqa: BLE001 — rule 5
            return {"body": "", "versions": [], "unstored": False}
        return {
            "body": body,
            # Each version beside what changed to produce it. `changes` against the
            # version before it, so the newest entry answers "what moved last".
            "versions": [
                {
                    "version": newer,
                    # The version before it, or None for the oldest one in the window —
                    # `changes(None, v)` renders the whole document as arrivals, which is
                    # what a first version is. Indexed rather than zipped: an empty
                    # history and a one-element one are the cases a paired iteration gets
                    # wrong, and both happen on a ledger nobody has synced yet.
                    "changes": situation.changes(
                        history[i + 1] if i + 1 < len(history) else None, newer
                    ),
                }
                for i, newer in enumerate(history)
            ],
            "unstored": bool(body) and (not history or history[0].body != body),
        }

    def page(request: Request, conn: sqlite3.Connection) -> Any:
        all_facts = facts.recall(conn)
        subjects: dict[str, list[facts.Fact]] = {}
        for f in all_facts:
            subjects.setdefault(f.subject, []).append(f)
        # The same subject, in the vault. `backglass vault export` writes one note per
        # lane, so the page and the vault are two views of one row set and the owner
        # should be able to step between them — Obsidian is where the backlinks are, this
        # is where the writes happen. Absent entirely when no vault is configured; a link
        # to a file that was never exported is worse than no link.
        vault_name = (
            Path(settings.vault_export_path).expanduser().name
            if settings.vault_export_path
            else None
        )
        vault_files = (
            {subject: f"Facts/{vault.safe_name(subject)}" for subject in subjects}
            if vault_name
            else {}
        )
        return templates.TemplateResponse(
            request,
            "memory.html",
            {
                "subjects": subjects,
                "vault_name": vault_name,
                "vault_files": vault_files,
                # What is actually on disk. The per-lane links below have always been
                # optimistic: they point into a vault whose last export the page never
                # stated, so a link into a folder written three weeks ago looked exactly
                # like one written this morning.
                "vault_state": vault.status(settings),
                "count": len(all_facts),
                # Extraction candidates behind the poison gate: visible here,
                # invisible to owner_context until accepted (rule 2 for memory).
                "pending": facts.proposed(conn),
                "situation": state_doc(conn),
                "settings": settings,
            },
        )

    @router.get("/memory", response_class=HTMLResponse)
    def memory(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
        return page(request, conn)

    @router.post("/memory", response_class=HTMLResponse)
    def add(
        request: Request,
        subject: str = Form(...),
        key: str = Form(...),
        value: str = Form(...),
        note: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        try:
            facts.remember(conn, settings, subject, key, value, note=note, source="manual")
        except facts.FactError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return page(request, conn)

    @router.post("/memory/{fact_id}/accept", response_class=HTMLResponse)
    def accept(
        fact_id: RowId, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Any:
        try:
            facts.accept(conn, settings, fact_id)
        except facts.FactError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return page(request, conn)

    @router.post("/memory/{fact_id}/reject", response_class=HTMLResponse)
    def reject(
        fact_id: RowId, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Any:
        try:
            facts.reject(conn, fact_id)
        except facts.FactError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return page(request, conn)

    @router.post("/memory/{fact_id}/forget", response_class=HTMLResponse)
    def forget(
        fact_id: RowId, request: Request, conn: sqlite3.Connection = Depends(get_conn)
    ) -> Any:
        try:
            facts.forget(conn, fact_id)
        except facts.FactError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return page(request, conn)

    return router
