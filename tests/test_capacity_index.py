"""The calendar-day read uses its index, asserted through the query plan.

Migration 0031 exists because `capacity.fixed_events` was scanning all 10,533
`source_item` rows to find at most 317 calendar ones — and its callers ask per day across
a horizon, so `questions.detect` paid for 46 of those scans per refresh and `logic.check`
42. Measured on a copy of the live ledger on 2026-08-23:

    logic.run          596 ms  →   8 ms
    questions.refresh  613 ms  →  65 ms
    planner.propose    282 ms  →  26 ms

A performance fix with no test is a performance fix with an expiry date. Nothing about
the *results* changes when the index stops being used, so every other test in the suite
keeps passing while the loop quietly goes back to spending 1.2 seconds of every
half-hourly sync re-reading ten thousand emails to find a Tuesday.

SQLite matches an expression index only when the predicate is written exactly as the
index declares it. That makes the risk concrete and boring: someone tidies
`datetime(occurred_at) >= datetime(?)` into something equivalent-looking, and the plan
silently reverts to a scan. So the test reads the plan, and it reads it for the query the
code actually issues — `capacity.CALENDAR_DAY_SQL` — not for a copy pasted in here, which
would keep passing after the code diverged from it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from backglass.db import connect, migrate
from backglass.ledger import USER_ID
from backglass.plan import capacity

INDEX = "idx_source_calendar_instant"


def _migrated(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "plan.db")
    migrate(conn)
    return conn


def _plan(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "EXPLAIN QUERY PLAN " + capacity.CALENDAR_DAY_SQL,
        (USER_ID, "2026-08-18T00:00:00-07:00", "2026-08-19T00:00:00-07:00"),
    ).fetchall()
    return " | ".join(str(r["detail"]) for r in rows)


def test_the_calendar_day_read_searches_the_index_instead_of_scanning(
    tmp_path: Path,
) -> None:
    conn = _migrated(tmp_path)
    try:
        plan = _plan(conn)
        assert INDEX in plan, plan
        assert "SCAN" not in plan, plan
    finally:
        conn.close()


def test_the_plan_binds_the_expression_and_not_only_the_partial_where(
    tmp_path: Path,
) -> None:
    """The check that actually has teeth, found by trying to break the one above.

    Rewriting the predicate to `occurred_at >= ?` — dropping the `datetime()` wrapper and
    with it both the index match *and* the timezone correctness — still leaves SQLite
    using this index, because a partial index can be chosen for its WHERE clause alone:

        as shipped          SEARCH ... USING INDEX ... (user_id=? AND <expr>>? AND <expr><?)
        predicate rewritten SEARCH ... USING INDEX ... (user_id=?)

    Both contain the index name and neither says SCAN, so "the index is used" passes on
    the broken one. The expression bounds are the difference between an indexed range
    read and a walk of every calendar row the ledger has ever held.
    """
    conn = _migrated(tmp_path)
    try:
        plan = _plan(conn)
        assert plan.count("<expr>") == 2, plan
    finally:
        conn.close()


def test_the_index_is_partial_so_the_ten_thousand_non_calendar_rows_pay_nothing(
    tmp_path: Path,
) -> None:
    """317 of 10,533 rows on the live ledger are calendar rows. Indexing the rest would
    cost a write on every ingest for a query that can never want them."""
    conn = _migrated(tmp_path)
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (INDEX,)
        ).fetchone()
        assert sql is not None, f"{INDEX} is missing"
        assert "WHERE source LIKE 'calendar%'" in str(sql["sql"])
    finally:
        conn.close()


def test_it_returns_the_same_events_the_scan_returned(tmp_path: Path) -> None:
    """The index must not change the answer, and the instant comparison is the reason to
    check rather than assume: an event at 18:00 Phoenix is 01:00 UTC the *next* day, and
    a `date()`-based read drops it from its own day. That is the bug the awkward
    predicate exists to avoid, and the index has to preserve it."""
    from datetime import date

    conn = _migrated(tmp_path)
    try:
        for external, occurred, title in (
            ("evening", "2026-08-18T18:00:00-07:00", "Evening seminar"),
            ("morning", "2026-08-18T09:00:00-07:00", "Morning class"),
            ("next-day", "2026-08-19T09:00:00-07:00", "Tomorrow"),
        ):
            conn.execute(
                "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
                " occurred_at, title, body_text, raw_json, content_hash, triage_verdict)"
                " VALUES (?, 'calendar:apple', ?, '2026-08-10T00:00:00Z', ?, ?, '',"
                " json_object('starts_at', ?, 'ends_at', ?), ?, 'keep')",
                (USER_ID, external, occurred, title, occurred, occurred, f"h-{external}"),
            )

        titles = [
            e.title
            for e in capacity.fixed_events(conn, date(2026, 8, 18), "America/Phoenix")
        ]
        assert sorted(titles) == ["Evening seminar", "Morning class"]
    finally:
        conn.close()
