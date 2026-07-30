"""Search, profile, and timeline reads. Phase 6.

Every timeline row and open commitment carries source_item columns, because a
profile is a view over evidence, not a CRM record with a memory of its own.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from backglass.db import query
from backglass.ledger import USER_ID


def _like(term: str | None) -> str | None:
    if term is None or not term.strip():
        return None
    return f"%{term.strip().lower()}%"


def search(
    conn: sqlite3.Connection, *, q: str | None = None, tag: str | None = None
) -> list[dict[str, Any]]:
    """LIKE over name, aliases, role, org, tags. Empty query lists everyone."""
    tag_like = f'%"{tag.strip().lower()}"%' if tag and tag.strip() else None
    rows = conn.execute(
        query("people_search"),
        {"user_id": USER_ID, "q": _like(q), "tag": tag_like},
    ).fetchall()
    out = []
    for row in rows:
        record = dict(row)
        record["tags"] = json.loads(record.pop("tags_json") or "[]")
        record["aliases"] = json.loads(record.pop("aliases_json") or "[]")
        out.append(record)
    return out


def profile(conn: sqlite3.Connection, entity_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM entity WHERE id = ? AND user_id = ?", (entity_id, USER_ID)
    ).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["tags"] = json.loads(record.pop("tags_json") or "[]")
    record["aliases"] = json.loads(record.pop("aliases_json") or "[]")
    return record


def timeline(conn: sqlite3.Connection, entity_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            query("people_timeline"), {"user_id": USER_ID, "entity_id": entity_id}
        )
    ]


def open_commitments(conn: sqlite3.Connection, entity_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            query("people_open_commitments"), {"user_id": USER_ID, "entity_id": entity_id}
        )
    ]
