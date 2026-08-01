"""Where the model spend goes. Read-only reporting over the `run` ledger.

Enforcement (sync.SpendCap) and reporting deliberately never share a query, but they
must share a clock: `_month_start` here is the same UTC computation SpendCap uses, so
the two views can never disagree about which month a run belongs to.

Honesty rule, printed wherever the numbers go: spend is recorded per run, not per
item. Anything below the run level — cost per item, cost per sender — is items × the
month's average, an estimate labelled as one.
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from backglass.config import Settings
from backglass.db import query
from backglass.ledger import USER_ID


@dataclass(frozen=True)
class MonthCosts:
    spend_cents: int
    cap_cents: int
    runs: int
    degraded_runs: int
    extracted: int
    fetched: int
    triaged_out: int
    avg_cents_per_item: float
    projected_cents: int
    days_elapsed: int
    days_in_month: int


def _month_start(today: date) -> str:
    """First instant of `today`'s month, ISO, UTC — identical shape to SpendCap's."""
    return datetime(today.year, today.month, 1, tzinfo=UTC).isoformat()


def month(
    conn: sqlite3.Connection, settings: Settings, today: date | None = None
) -> MonthCosts:
    today = today or datetime.now(UTC).date()
    row = conn.execute(
        query("costs_month"), {"user_id": USER_ID, "month_start": _month_start(today)}
    ).fetchone()
    spend = int(row["spend_cents"])
    extracted = int(row["extracted"])
    days_elapsed = today.day
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    return MonthCosts(
        spend_cents=spend,
        cap_cents=settings.monthly_spend_cap_cents,
        runs=int(row["runs"]),
        degraded_runs=int(row["degraded_runs"]),
        extracted=extracted,
        fetched=int(row["fetched"]),
        triaged_out=int(row["triaged_out"]),
        avg_cents_per_item=(spend / extracted) if extracted else 0.0,
        projected_cents=round(spend / days_elapsed * days_in_month),
        days_elapsed=days_elapsed,
        days_in_month=days_in_month,
    )


def by_run(conn: sqlite3.Connection, limit: int = 15) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(query("costs_by_run"), {"user_id": USER_ID, "limit": limit})
    ]


def kill_trend(conn: sqlite3.Connection, weeks: int = 4) -> list[dict[str, Any]]:
    """Weekly kill rate, newest first. Below 0.85 is the rules-drifted signal."""
    return [
        dict(r)
        for r in conn.execute(query("costs_kill_trend"), {"user_id": USER_ID, "weeks": weeks})
    ]


def top_senders(
    conn: sqlite3.Connection,
    month_costs: MonthCosts,
    today: date | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Surviving senders by extraction volume, each with its estimated share of spend."""
    today = today or datetime.now(UTC).date()
    rows = [
        dict(r)
        for r in conn.execute(
            query("costs_top_senders"),
            {"user_id": USER_ID, "month_start": _month_start(today), "limit": limit},
        )
    ]
    for row in rows:
        row["approx_cents"] = round(row["items_extracted"] * month_costs.avg_cents_per_item)
    return rows
