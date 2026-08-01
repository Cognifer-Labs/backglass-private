"""Days-since-last-touch, mirroring goals/health.Staleness on purpose.

Same shape, same chip discipline (a day count, never a bare colour), separate type —
a person going quiet and a goal going quiet need different responses, which is the
same reason health.py refuses to merge staleness and risk.

Only curated profiles participate (role, org, or tags set) — the query enforces it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

from backglass.config import Settings
from backglass.db import query
from backglass.ledger import USER_ID


@dataclass(frozen=True)
class Touch:
    entity_id: int
    name: str
    role: str | None
    org: str | None
    days_since: int | None  # None when there has never been an interaction
    level: str  # new | fresh | warn | cold
    # Provenance of the most recent interaction; None only when days_since is None.
    source_row: dict[str, Any] | None

    def chip(self) -> str:
        if self.days_since is None:
            return "no interactions yet"
        return f"{self.days_since} days since last touch"


def cold(conn: sqlite3.Connection, settings: Settings, day: date) -> list[Touch]:
    """All curated profiles with their touch level, coldest first."""
    out: list[Touch] = []
    for row in conn.execute(query("people_cold"), {"user_id": USER_ID}):
        if row["last_touch_at"]:
            days: int | None = (day - date.fromisoformat(str(row["last_touch_at"])[:10])).days
        else:
            days = None
        if days is None:
            # Never interacted is young data, not a lapsed relationship: a class
            # roster imported yesterday must not render as a wall of alarm ink.
            # Format-audit ruling: vermilion means broken/overdue, nothing else.
            level = "new"
        elif days >= settings.people_touch_cold_days:
            level = "cold"
        elif days >= settings.people_touch_warn_days:
            level = "warn"
        else:
            level = "fresh"
        source_row = (
            {
                "source_item_id": row["source_item_id"],
                "source": row["source"],
                "source_external_id": row["source_external_id"],
                "source_occurred_at": row["source_occurred_at"],
                "source_title": row["source_title"],
            }
            if row["last_touch_at"]
            else None
        )
        out.append(
            Touch(
                entity_id=int(row["id"]),
                name=str(row["canonical_name"]),
                role=row["role"],
                org=row["org"],
                days_since=days,
                level=level,
                source_row=source_row,
            )
        )
    return out


def needing_follow_up(
    conn: sqlite3.Connection, settings: Settings, day: date
) -> list[Touch]:
    """Warn-or-worse, with provenance — the brief's nudge source. Coldest first."""
    return [
        t
        for t in cold(conn, settings, day)
        if t.level in ("warn", "cold") and t.source_row is not None
    ]
