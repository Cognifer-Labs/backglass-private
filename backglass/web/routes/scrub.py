"""The Scrub page: the whole neglected board, cleared in one pass.

The counterpart to `/ask`, and deliberately its opposite. Ask shows one question at a time
because a considered answer needs room. Scrub shows eighty-seven rows at once because the
owner did not come here to consider anything — they came because the board is full of
things they finished weeks ago and the five-a-day drip will never reach the fishtank.

Every button here is one the dashboard already has: `resolve`, `drop`, `snooze`. Nothing
in this file decides anything, and `backglass.scrub` only detects, so the entire surface
is a faster way to press buttons the owner could already press one at a time. That is the
point — the disposal rules that can act alone live in `logic.py`, and everything that
reaches this page is specifically what the machine could not justify closing by itself.

Rows act in place over HTMX and the page is not re-rendered between them, so a pass down
the list never loses the owner's scroll position. A row already closed by another surface
answers 422 and says so rather than erroring, because two tabs open on a full board is a
normal thing to have.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from backglass import scrub
from backglass.config import Settings
from backglass.web import actions

#: What "keep" means in days. Long enough that the row does not come back in the same
#: month's scrubbing, short enough to stay a snooze rather than a silent drop — the owner
#: said this one is real, not that it is finished.
KEEP_DAYS = 30


def _keep_days(conn: sqlite3.Connection, commitment_id: int, today: date) -> int:
    """How many days to snooze so the row lands `KEEP_DAYS` from *now*.

    `actions.snooze` counts from the commitment's existing due date, which is right for
    the board — "push tomorrow's thing to the day after" — and wrong for every row on this
    page, because every row on this page is already past due. Keeping the fishtank due
    2026-01-06 for thirty days would set it to 2026-02-05: still overdue, still stale, and
    back on the scrub board the instant the page reloaded. The button would have lied.

    So the arrears are added back. A row with no due date has nothing to count from and
    `snooze` bases it on now, where the plain figure is already correct.
    """
    row = conn.execute(
        "SELECT due_at FROM commitment WHERE id = ?", (commitment_id,)
    ).fetchone()
    if row is None or not row["due_at"]:
        return KEEP_DAYS
    try:
        due = date.fromisoformat(str(row["due_at"])[:10])
    except ValueError:
        return KEEP_DAYS
    return max(KEEP_DAYS, (today - due).days + KEEP_DAYS)


def build_router(
    templates: Any,
    settings: Settings,
    get_conn: Callable[[], Any],
    today: Callable[[], date],
) -> APIRouter:
    router = APIRouter()

    @router.get("/scrub", response_class=HTMLResponse)
    def page(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
        groups = scrub.board(conn, today())
        plan_pairs, plan_total = scrub.duplicate_plans(conn, settings)
        return templates.TemplateResponse(
            request,
            "scrub.html",
            {
                "groups": groups,
                "total": sum(len(group.rows) for group in groups),
                # Plans are a second kind of row, not a fourth group: a group holds
                # commitments and offers done/drop/keep, and a pair holds two plans and
                # offers merge/keep-both. Folding them together would mean one template
                # branch per button on every row.
                "plan_pairs": plan_pairs,
                "plan_total": plan_total,
                "today": today(),
                "keep_days": KEEP_DAYS,
            },
        )

    @router.post("/scrub/plans/{a_id}/{b_id}/{verdict}", response_class=HTMLResponse)
    def act_on_pair(
        a_id: int,
        b_id: int,
        verdict: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """One suspected pair, one verdict. Returns the fragment that replaces the row.

        `same` folds the newer plan into the older; `apart` remembers the pair so it is
        never offered again. Neither is destructive — a merged plan is `superseded` with
        its evidence carried across, and one status flip puts it back.
        """
        if verdict not in ("same", "apart"):
            return HTMLResponse("<span class='scrubdone'>unknown verdict</span>", 400)
        try:
            if verdict == "same":
                actions.same_plan(conn, a_id, b_id)
                said = "merged"
            else:
                actions.different_plans(conn, a_id, b_id)
                said = "kept apart"
        except actions.ActionError as exc:
            return HTMLResponse(f"<span class='scrubdone'>{exc}</span>")
        conn.commit()
        return HTMLResponse(f"<span class='scrubdone'>{said}</span>")

    @router.post("/scrub/{commitment_id}/{verdict}", response_class=HTMLResponse)
    def act(
        commitment_id: int,
        verdict: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        """One row, one verdict. Returns the fragment that replaces the row.

        `done` and `drop` are the same two actions the board offers and carry a note
        naming this surface, so a year from now the resolution reads as a decision the
        owner made in a scrubbing pass rather than as something that just happened.
        """
        if verdict not in ("done", "drop", "keep"):
            return HTMLResponse("<span class='scrubdone'>unknown verdict</span>", 400)
        note = "scrub: owner cleared the board"
        try:
            if verdict == "done":
                actions.resolve(conn, commitment_id, note=note)
                said = "done"
            elif verdict == "drop":
                actions.drop(conn, commitment_id, note=note)
                said = "dropped"
            else:
                actions.snooze(
                    conn, commitment_id, days=_keep_days(conn, commitment_id, today())
                )
                said = f"kept — back in {KEEP_DAYS} days"
        except actions.ActionError as exc:
            # Already closed in another tab. Not an error worth a red banner: the owner
            # wanted it off the board and it is off the board.
            conn.commit()
            return HTMLResponse(f"<span class='scrubdone'>{exc}</span>", 422)
        conn.commit()
        return HTMLResponse(f"<span class='scrubdone'>{said}</span>")

    return router
