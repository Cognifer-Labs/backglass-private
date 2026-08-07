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
from datetime import UTC, date, datetime, timedelta
from typing import Any

from backglass.config import Settings
from backglass.db import query
from backglass.extract import client
from backglass.ledger import USER_ID


@dataclass(frozen=True)
class MonthCosts:
    spend_cents: int
    cap_cents: int
    runs: int
    degraded_runs: int
    #: `degraded_runs` split by cause, because the two pauses have nothing to do with each
    #: other and the summary has to name the right one. They sum to `degraded_runs`; a
    #: pre-0016 row counts as the cap, which is the only thing it could have been.
    capped_runs: int
    rate_limited_runs: int
    extracted: int
    fetched: int
    triaged_out: int
    avg_cents_per_item: float
    projected_cents: int
    days_elapsed: int
    days_in_month: int
    #: True when the configured backend reports a price rather than a charge — a
    #: subscription. Every number above is then what the month *would have* cost on the
    #: API, and the cap is not enforced against it. Carried here rather than looked up by
    #: each caller so no surface can print "of cap" beside a figure nobody is billed.
    spend_is_imputed: bool = False


def _month_start(today: date) -> str:
    """First instant of `today`'s month, ISO, UTC — identical shape to SpendCap's."""
    return datetime(today.year, today.month, 1, tzinfo=UTC).isoformat()


def cap_resets_on(today: date | None = None) -> date:
    """The day the cap's budget starts over — the first of the month after `today`'s.

    Derived from `_month_start` rather than computed afresh, for the reason in the module
    docstring: enforcement and reporting share one clock. "Extraction is paused" is only
    half a sentence without this date; the cap has no other release valve, so a run that
    hits it on the 3rd is telling the owner about four silent weeks.
    """
    start = datetime.fromisoformat(_month_start(today or datetime.now(UTC).date()))
    return date(start.year + start.month // 12, start.month % 12 + 1, 1)


def stranded_extractions(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Kept items with no extraction at the current prompt version.

    What the cap is actually costing, in items rather than cents: every one of these is a
    triaged-in message whose commitments are not in the ledger, and the ledger is the
    product. See stranded_extractions.sql for why the predicate is sync's, not a looser one.
    """
    from backglass.extract import prompts
    from backglass.sync import EXTRACT_PROMPT

    cutoff = (now or datetime.now(UTC)) - timedelta(hours=26)
    row = conn.execute(
        query("stranded_extractions"),
        {
            "user_id": USER_ID,
            "extraction_version": prompts.load(EXTRACT_PROMPT).stamp,
            "cutoff": cutoff.isoformat(),
        },
    ).fetchone()
    return int(row["stranded"])


def untriaged_items(conn: sqlite3.Connection) -> int:
    """Ingested items with no triage verdict yet — what a paused *triage* is costing.

    The stranded count above cannot see these: they have no verdict, so no predicate over
    'keep' reaches them. See untriaged_items.sql.
    """
    row = conn.execute(query("untriaged_items"), {"user_id": USER_ID}).fetchone()
    return int(row["untriaged"])


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
        capped_runs=int(row["capped_runs"]),
        rate_limited_runs=int(row["rate_limited_runs"]),
        extracted=extracted,
        fetched=int(row["fetched"]),
        triaged_out=int(row["triaged_out"]),
        avg_cents_per_item=(spend / extracted) if extracted else 0.0,
        projected_cents=round(spend / days_elapsed * days_in_month),
        days_elapsed=days_elapsed,
        days_in_month=days_in_month,
        spend_is_imputed=client.spend_is_imputed(settings),
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


def by_call_tier(conn: sqlite3.Connection, days: int = 7) -> list[dict[str, Any]]:
    """What a call in each tier actually costs and takes. Phase 0 of the backend plan.

    Medians and p95 rather than means: the distribution here is what the plan turns on.
    A mean hides the difference between "every call pays a fixed session overhead" and
    "a few big payloads dominate", and those two readings point at opposite fixes —
    batching in the first case, truncation in the second.

    Percentiles are computed in SQLite with a window function rather than in Python so
    the whole readout stays one query; `NTILE` is avoided because it splits ties
    arbitrarily on small samples, which is exactly the sample size this will have in its
    first week.
    """
    return [
        dict(r)
        for r in conn.execute(
            """
            WITH ranked AS (
              SELECT tier, duration_ms, cost_usd, prompt_chars, outcome,
                     ROW_NUMBER() OVER (PARTITION BY tier ORDER BY duration_ms) AS rn,
                     COUNT(*)     OVER (PARTITION BY tier)                      AS n
              FROM model_call
              WHERE user_id = :user_id AND started_at >= :since
            )
            SELECT tier,
                   n                                                   AS calls,
                   SUM(outcome != 'ok')                                AS failures,
                   ROUND(AVG(prompt_chars))                            AS mean_chars,
                   MAX(CASE WHEN rn = (n + 1) / 2 THEN duration_ms END) AS median_ms,
                   MAX(CASE WHEN rn = MAX(1, CAST(n * 0.95 AS INTEGER))
                            THEN duration_ms END)                       AS p95_ms,
                   ROUND(SUM(cost_usd) * 100, 2)                        AS cents,
                   ROUND(SUM(cost_usd) * 100 / n, 3)                    AS cents_per_call
            FROM ranked GROUP BY tier ORDER BY cents DESC
            """,
            {"user_id": USER_ID, "since": _days_ago(days)},
        )
    ]


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()
