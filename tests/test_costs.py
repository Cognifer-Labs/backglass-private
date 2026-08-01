"""The costs report: read-only, month-windowed, honest about attribution.

Projection arithmetic runs against a pinned `today` — the interview suite already
paid the price of a date-fragile test once.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from backglass import costs
from backglass.config import Settings
from backglass.db import query

TODAY = date(2026, 8, 10)  # day 10 of a 31-day month


def _run_row(
    conn: sqlite3.Connection,
    started_at: str,
    spend_cents: int,
    extracted: int = 0,
    degraded: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO run (user_id, started_at, spend_cents, items_extracted,"
        " items_fetched, items_triaged_out, degraded)"
        " VALUES (1, ?, ?, ?, ?, ?, ?)",
        (started_at, spend_cents, extracted, extracted + 5, 5, degraded),
    )


def _extracted_item(
    conn: sqlite3.Connection, author: str, n: int, commitments: int = 0
) -> None:
    for i in range(n):
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, author, title, body_text, raw_json, content_hash,"
            " triage_verdict, triage_reason, extraction_version)"
            " VALUES (1, 'gmail:t', ?, '2026-08-05T00:00:00+00:00',"
            " '2026-08-05T00:00:00+00:00', ?, 't', 'b', '{}', ?, 'keep', 'model', 'v1')",
            (f"{author}:{i}", author, f"h:{author}:{i}"),
        )
        item_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        for _ in range(commitments):
            conn.execute(
                "INSERT INTO commitment (user_id, direction, what, confidence, status,"
                " source_item_id, created_at)"
                " VALUES (1, 'i_owe', 'x', 0.9, 'open', ?, '2026-08-05T00:00:00')",
                (item_id,),
            )


class TestMonth:
    def test_sums_only_the_current_month_and_projects(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _run_row(conn, "2026-07-30T05:00:00+00:00", 900)  # July: excluded
        _run_row(conn, "2026-08-02T05:00:00+00:00", 300, extracted=10)
        _run_row(conn, "2026-08-09T05:00:00+00:00", 200, extracted=10, degraded=1)

        m = costs.month(conn, settings, today=TODAY)

        assert m.spend_cents == 500
        assert m.runs == 2
        assert m.degraded_runs == 1
        assert m.extracted == 20
        assert m.avg_cents_per_item == 25.0
        # 500c over 10 days of 31 → 1550 by month end.
        assert m.projected_cents == 1550
        assert m.days_elapsed == 10 and m.days_in_month == 31

    def test_empty_month_has_no_division_by_zero(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        m = costs.month(conn, settings, today=TODAY)
        assert m.spend_cents == 0
        assert m.avg_cents_per_item == 0.0
        assert m.projected_cents == 0


class TestSenders:
    def test_approx_cost_is_items_times_month_average(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _run_row(conn, "2026-08-02T05:00:00+00:00", 400, extracted=8)
        _extracted_item(conn, "canvas@instructure.com", 6, commitments=2)
        _extracted_item(conn, "dana@example.com", 2, commitments=1)
        # Dropped and unextracted items never appear.
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, author, content_hash, triage_verdict)"
            " VALUES (1, 'gmail:t', 'dropped', '2026-08-05T00:00:00+00:00',"
            " '2026-08-05T00:00:00+00:00', 'noise@x.com', 'h:dropped', 'drop')"
        )

        m = costs.month(conn, settings, today=TODAY)
        senders = costs.top_senders(conn, m, today=TODAY)

        assert [s["author"] for s in senders] == [
            "canvas@instructure.com",
            "dana@example.com",
        ]
        # avg = 400c/8 = 50c → 6 items ≈ 300c, 2 items ≈ 100c.
        assert senders[0]["approx_cents"] == 300
        assert senders[0]["commitments"] == 12  # 6 items × 2 commitments joined
        assert senders[1]["approx_cents"] == 100


class TestTrendAndRuns:
    def test_kill_trend_buckets_by_week(self, conn: sqlite3.Connection) -> None:
        for i, (day, verdict) in enumerate(
            [("2026-08-03", "drop"), ("2026-08-04", "drop"), ("2026-08-05", "keep"),
             ("2026-08-10", "drop")]
        ):
            conn.execute(
                "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
                " occurred_at, author, content_hash, triage_verdict)"
                " VALUES (1, 'gmail:t', ?, ?, ?, 'a@b.c', ?, ?)",
                (f"kt{i}", f"{day}T00:00:00+00:00", f"{day}T00:00:00+00:00",
                 f"h:kt{i}", verdict),
            )
        trend = costs.kill_trend(conn, weeks=4)
        assert len(trend) == 2  # two ISO weeks touched
        newest = trend[0]
        assert newest["total"] == 1 and newest["dropped"] == 1
        older = trend[1]
        assert older["total"] == 3 and older["dropped"] == 2

    def test_by_run_returns_newest_first(self, conn: sqlite3.Connection) -> None:
        _run_row(conn, "2026-08-01T05:00:00+00:00", 100)
        _run_row(conn, "2026-08-02T05:00:00+00:00", 200)
        rows = costs.by_run(conn, limit=1)
        assert len(rows) == 1
        assert rows[0]["spend_cents"] == 200


def test_every_costs_query_loads_and_executes(conn: sqlite3.Connection) -> None:
    """Smoke: SQL typos die here, not in the CLI."""
    params = {"user_id": 1, "month_start": "2026-08-01T00:00:00+00:00", "limit": 5, "weeks": 4}
    for name in ("costs_month", "costs_by_run", "costs_daily", "costs_kill_trend",
                 "costs_top_senders"):
        sql = query(name)
        needed = {k: v for k, v in params.items() if f":{k}" in sql}
        conn.execute(sql, needed).fetchall()
