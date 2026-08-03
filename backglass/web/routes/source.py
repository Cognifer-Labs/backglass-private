"""The source page: one raw item and everything derived from it.

CLAUDE.md rule 1 is "every generated claim links to its source". The links existed; the
page they pointed at did not (`brief/model.py` built `/source/<external_id>` and nothing
served it), so the rule held on the writing side and failed on the reading side for every
source except Gmail — which is to say, for the entire ledger as it stands today.

Read-only, deliberately. Everything on this page is either immutable (docs/03) or owned
by another surface's write path, and a page whose job is "show me what this claim rests
on" should not be able to change what it rests on.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from backglass.config import Settings
from backglass.db import query
from backglass.ledger import USER_ID


def build_router(
    templates: Jinja2Templates,
    settings: Settings,
    get_conn: Callable[[], Any],
    today: Callable[[], date],
) -> APIRouter:
    del today  # a source item is dated by when it happened, not by when it is read
    router = APIRouter()

    @router.get("/source/{source_item_id}", response_class=HTMLResponse)
    def source_item(
        source_item_id: int,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> Any:
        item = conn.execute(
            query("source_item_detail"), {"user_id": USER_ID, "id": source_item_id}
        ).fetchone()
        if item is None:
            # An honest 404 rather than an empty page: a provenance link that silently
            # renders "nothing here" is the failure this whole change exists to remove.
            raise HTTPException(status_code=404, detail=f"no source item {source_item_id}")

        derived = list(
            conn.execute(
                query("source_item_derived"), {"user_id": USER_ID, "id": source_item_id}
            )
        )
        facts = list(
            conn.execute(
                "SELECT id, subject, key, value, note, status FROM fact "
                "WHERE user_id = ? AND source_item_id = ? ORDER BY id",
                (USER_ID, source_item_id),
            )
        )
        return templates.TemplateResponse(
            request,
            "source.html",
            {
                "item": item,
                "derived": derived,
                "facts": facts,
                "external_url": _external_url(item),
                "settings": settings,
            },
        )

    return router


def _external_url(item: dict[str, Any]) -> str | None:
    """The originating account, where the source has one a browser can open.

    Same rule the dashboard's `source_link` macro follows: name the source without a
    href rather than invent one. A dead link is worse than no link.
    """
    source = str(item["source"] or "")
    if source.startswith("gmail"):
        return f"https://mail.google.com/mail/u/0/#all/{item['external_id']}"
    return None
