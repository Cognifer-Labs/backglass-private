"""Reading the review queue's verdicts back.

Not one `resolution_note` string in this file is written by hand. Every fixture is
produced by calling `web.actions.accept` / `web.actions.reject`, because the whole
report is a parser over a format that lives in another module: a hand-typed
'rejected:wrong_date' would keep passing on the day actions.py starts writing something
else, and the parser would go silently blind against the real database. Going through
the actions is the only version of this test that can fail when the format drifts.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from backglass import corrections
from backglass.web import actions

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def _item(
    conn: sqlite3.Connection,
    n: int,
    *,
    source: str = "gmail",
    author: str = "a@example.com",
    version: str | None = "v3",
) -> int:
    cur = conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, content_hash, triage_verdict, extraction_version)"
        " VALUES (1, ?, ?, '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00',"
        " ?, 't', 'b', ?, 'keep', ?)",
        (source, f"ext:{source}:{n}", author, f"hash:{source}:{n}", version),
    )
    return int(cur.lastrowid or 0)


def _commitment(
    conn: sqlite3.Connection,
    n: int,
    confidence: float,
    *,
    source: str = "gmail",
    author: str = "a@example.com",
    version: str | None = "v3",
    created_at: str = "2026-08-01T09:00:00+00:00",
) -> int:
    item_id = _item(conn, n, source=source, author=author, version=version)
    cur = conn.execute(
        "INSERT INTO commitment (user_id, direction, what, confidence, status,"
        " source_item_id, created_at) VALUES (1, 'i_owe', ?, ?, 'open', ?, ?)",
        (f"thing {n}", confidence, item_id, created_at),
    )
    return int(cur.lastrowid or 0)


def _reject(
    conn: sqlite3.Connection, n: int, confidence: float, reason: str, **kw: object
) -> int:
    cid = _commitment(conn, n, confidence, **kw)  # type: ignore[arg-type]
    actions.reject(conn, cid, reason)
    return cid


def _accept(conn: sqlite3.Connection, n: int, confidence: float, **kw: object) -> int:
    cid = _commitment(conn, n, confidence, **kw)  # type: ignore[arg-type]
    actions.accept(conn, cid)
    return cid


class TestEmptyAndThin:
    def test_empty_db_reports_nothing_rather_than_zeroes(
        self, conn: sqlite3.Connection
    ) -> None:
        rep = corrections.report(conn, days=30, now=NOW)
        assert rep.total == 0
        assert rep.buckets == []
        assert rep.dominant_reason is None
        assert rep.remedy is None
        assert not rep.meaningful

    def test_uncorrected_resolutions_are_not_corrections(
        self, conn: sqlite3.Connection
    ) -> None:
        """`resolve`/`drop` write free text into the same column. It is not a verdict."""
        done = _commitment(conn, 1, 0.9)
        actions.resolve(conn, done, note="finished it")
        gone = _commitment(conn, 2, 0.9)
        actions.drop(conn, gone, note="rejected: because I said so")
        assert corrections.report(conn, days=30, now=NOW).total == 0

    def test_thin_sample_is_flagged_but_still_counted(self, conn: sqlite3.Connection) -> None:
        for i in range(14):
            _reject(conn, i, 0.6, "wrong_date")
        rep = corrections.report(conn, days=30, now=NOW)
        assert rep.total == 14
        assert not rep.meaningful
        # The shape is still computed — the CLI is what declines to print it, so the
        # numbers stay available to anything that knows what it is asking for.
        assert rep.reasons == {"wrong_date": 14}


class TestReasonDistribution:
    def test_counts_per_reason_and_names_the_file_to_edit(
        self, conn: sqlite3.Connection
    ) -> None:
        for i in range(30):
            _reject(conn, i, 0.6, "wrong_date")
        for i in range(30, 40):
            _reject(conn, i, 0.6, "not_mine")
        for i in range(40, 55):
            _accept(conn, i, 0.65)

        rep = corrections.report(conn, days=30, now=NOW)
        assert rep.total == 55
        assert rep.accepts == 15
        assert rep.rejects == 40
        assert rep.meaningful
        assert rep.reasons == {"wrong_date": 30, "not_mine": 10}
        assert rep.dominant_reason == "wrong_date"
        assert "extract/dates.py" in (rep.remedy or "")

    def test_a_tie_points_at_no_file(self, conn: sqlite3.Connection) -> None:
        for i in range(6):
            _reject(conn, i, 0.6, "wrong_date")
        for i in range(6, 12):
            _reject(conn, i, 0.6, "not_a_commitment")
        rep = corrections.report(conn, days=30, now=NOW)
        assert rep.dominant_reason is None
        assert rep.remedy is None

    def test_splits_by_source_and_by_prompt_version(self, conn: sqlite3.Connection) -> None:
        for i in range(5):
            _reject(conn, i, 0.6, "wrong_date", source="gmail", version="v3")
        for i in range(5, 8):
            _reject(conn, i, 0.6, "not_a_commitment", source="canvas", version="v4")
        _accept(conn, 9, 0.6, source="canvas", version="v4")

        rep = corrections.report(conn, days=30, now=NOW)
        assert rep.reasons_by_source == {
            "gmail": {"wrong_date": 5},
            "canvas": {"not_a_commitment": 3},
        }
        assert rep.reasons_by_version == {
            "v3": {"wrong_date": 5},
            "v4": {"not_a_commitment": 3},
        }

    def test_an_unstamped_item_is_named_not_dropped(self, conn: sqlite3.Connection) -> None:
        """Quick-add rows carry `extraction_version = 'manual'`; older rows carry NULL."""
        _reject(conn, 1, 0.6, "not_mine", version=None)
        rep = corrections.report(conn, days=30, now=NOW)
        assert rep.reasons_by_version == {"<unstamped>": {"not_mine": 1}}


class TestCalibration:
    def test_buckets_the_models_own_score_not_the_post_accept_one(
        self, conn: sqlite3.Connection
    ) -> None:
        """accept() overwrites confidence with 1.0; only the note remembers 0.55."""
        cid = _accept(conn, 1, 0.55)
        stored = conn.execute(
            "SELECT confidence FROM commitment WHERE id = ?", (cid,)
        ).fetchone()
        assert float(stored["confidence"]) == 1.0

        rep = corrections.report(conn, days=30, now=NOW)
        assert [(b.low, b.accepted, b.rejected) for b in rep.buckets] == [(0.5, 1, 0)]

    def test_an_overconfident_band_is_visible(self, conn: sqlite3.Connection) -> None:
        # 0.9 band: the model is sure and mostly right. 0.7 band: sure enough to have
        # been believed, and wrong most of the time — the reason to move the threshold.
        for i in range(8):
            _accept(conn, i, 0.95)
        for i in range(8, 10):
            _reject(conn, i, 0.92, "wrong_date")
        for i in range(10, 12):
            _accept(conn, i, 0.72)
        for i in range(12, 20):
            _reject(conn, i, 0.75, "wrong_date")

        rep = corrections.report(conn, days=30, now=NOW)
        by_low = {b.low: b for b in rep.buckets}
        assert round(by_low[0.9].accept_rate, 2) == 0.80
        assert round(by_low[0.7].accept_rate, 2) == 0.20

    def test_bucket_edges_land_on_the_threshold(self, conn: sqlite3.Connection) -> None:
        """0.70 belongs with 0.79, not with 0.69 — the threshold band must be readable."""
        _accept(conn, 1, 0.70)
        _accept(conn, 2, 0.79)
        _accept(conn, 3, 0.69)
        rep = corrections.report(conn, days=30, now=NOW)
        assert [(b.low, b.total) for b in rep.buckets] == [(0.6, 1), (0.7, 2)]

    def test_a_perfect_score_stays_in_the_top_band(self, conn: sqlite3.Connection) -> None:
        _reject(conn, 1, 1.0, "not_mine")
        rep = corrections.report(conn, days=30, now=NOW)
        assert [(b.low, b.total) for b in rep.buckets] == [(0.9, 1)]


class TestWorstSources:
    def test_ranks_by_rejection_rate_then_volume(self, conn: sqlite3.Connection) -> None:
        for i in range(4):
            _reject(conn, i, 0.6, "not_mine", source="canvas", author="lms@example.edu")
        for i in range(4, 6):
            _accept(conn, i, 0.6, source="gmail", author="colleague@example.com")
        _reject(conn, 7, 0.6, "wrong_date", source="gmail", author="colleague@example.com")

        rep = corrections.report(conn, days=30, now=NOW)
        assert [(s.key, s.rejected, s.total) for s in rep.worst_sources] == [
            ("canvas", 4, 4),
            ("gmail", 1, 3),
        ]
        assert rep.worst_senders[0].key == "lms@example.edu"


class TestWindow:
    def test_only_the_trailing_window_counts(self, conn: sqlite3.Connection) -> None:
        # A reject stamps resolved_at = now(), so an old reject cannot be simulated by
        # backdating created_at — it is aged by rewriting resolved_at afterwards, which
        # is what actually decides the window for a rejection.
        old = _reject(conn, 1, 0.6, "wrong_date")
        conn.execute(
            "UPDATE commitment SET resolved_at = '2026-05-01T00:00:00+00:00' WHERE id = ?",
            (old,),
        )
        _reject(conn, 2, 0.6, "not_mine")

        rep = corrections.report(conn, days=30, now=NOW)
        assert rep.reasons == {"not_mine": 1}
        assert corrections.report(conn, days=180, now=NOW).total == 2

    def test_an_accept_is_windowed_on_created_at(self, conn: sqlite3.Connection) -> None:
        """accept() stamps no time at all, so `created_at` is the only proxy there is."""
        _accept(conn, 1, 0.6, created_at="2026-05-01T00:00:00+00:00")
        _accept(conn, 2, 0.6, created_at="2026-08-01T00:00:00+00:00")
        assert corrections.report(conn, days=30, now=NOW).total == 1
        assert corrections.report(conn, days=180, now=NOW).total == 2
