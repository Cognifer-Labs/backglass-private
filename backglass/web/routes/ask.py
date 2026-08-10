"""The Ask page: one confusion at a time, full screen, with room to answer in words.

Deliberately not a panel. Every other surface in this app is something the owner reads
past — the board, the queue, the schedule — and a question that scrolls past is a question
that does not get answered; the review queue reached 303 open rows proving it. So this one
takes the whole screen and shows exactly one question, because the cost of a decision is
mostly the cost of deciding which decision to make.

docs/04 §6 excludes nagging, and the line this stays on is: it does not push, ring, or
interrupt. It is a page the owner arrives at, offered from the dashboard when something is
waiting. Answering or dismissing every question returns them to their day and the page has
nothing further to say.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from backglass import questions
from backglass.config import Settings


def build_router(
    templates: Jinja2Templates,
    settings: Settings,
    get_conn: Callable[[], Any],
    today: Callable[[], date],
) -> APIRouter:
    router = APIRouter()

    def render(request: Request, conn: sqlite3.Connection) -> Any:
        # Refreshed on arrival rather than on a schedule: the detectors are read-only and
        # cheap, and a question surface that is stale is worse than one that is empty.
        questions.refresh(conn, settings, today())
        conn.commit()
        waiting = questions.open_questions(conn)
        return templates.TemplateResponse(
            request,
            "ask.html",
            {
                "question": waiting[0] if waiting else None,
                "remaining": len(waiting),
                "today": today(),
            },
        )

    @router.get("/ask", response_class=HTMLResponse)
    def ask(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
        return render(request, conn)

    @router.post("/ask/{question_id}/answer")
    def answer(
        question_id: int,
        option: str = Form(default=""),
        text: str = Form(default=""),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        # Words win when both arrive. A pressed button plus a typed sentence means the
        # owner had more to say than the button carried, and keeping the button as the
        # answer would throw away the part they bothered to write.
        try:
            questions.answer(
                conn,
                settings,
                question_id,
                option=option.strip() or None,
                text=text.strip() or None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        conn.commit()
        return RedirectResponse("/ask", status_code=303)

    @router.post("/ask/{question_id}/dismiss")
    def dismiss(question_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
        questions.dismiss(conn, question_id)
        conn.commit()
        return RedirectResponse("/ask", status_code=303)

    return router
