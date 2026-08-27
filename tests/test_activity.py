"""The Activity feed and its page.

Three tables written between 0030 and 0032 reached no surface for four days: 421 claim
events, 18 retractions and 28 notifications with no reader anywhere under `backglass/web`.
These are the tests for the reader that closed that, and for the one decision inside it
that is easy to get wrong — a change where the old value equals the new value is not a
change, and 94% of the owner's claim events are exactly that.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backglass import activity
from backglass.config import Settings
from backglass.db import now_iso
from backglass.web.app import create_app
from tests.conftest import panel_slice


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


@pytest.fixture(autouse=True)
def sync_is_alive(conn: sqlite3.Connection) -> None:
    from tests.conftest import healthy_run

    healthy_run(conn)
    conn.commit()


def _source_item(conn: sqlite3.Connection, *, source: str, title: str) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, content_hash)"
        " VALUES (1, ?, ?, ?, ?, ?, '', ?)",
        (source, f"ext-{title}", now_iso(), now_iso(), title, f"hash-{title}"),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _commitment(conn: sqlite3.Connection, what: str) -> int:
    item = _source_item(conn, source="test", title=f"src for {what}")
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, confidence, status,"
        " source_item_id, created_at) VALUES (1, 'i_owe', ?, 0.9, 'open', ?, ?)",
        (what, item, now_iso()),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _event(
    conn: sqlite3.Connection,
    *,
    subject_id: int,
    field: str,
    old: str | None,
    new: str | None,
    cause: str,
    at: str | None = None,
    subject_table: str = "commitment",
) -> None:
    conn.execute(
        "INSERT INTO claim_event (user_id, at, subject_table, subject_id, field,"
        " old_value, new_value, cause) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
        (at or now_iso(), subject_table, subject_id, field, old, new, cause),
    )


class TestUnchangedIsNotAChange:
    """The decision the whole feed rests on."""

    def test_an_event_whose_value_did_not_move_is_counted_not_listed(
        self, conn: sqlite3.Connection
    ) -> None:
        cid = _commitment(conn, "write the lab report")
        _event(conn, subject_id=cid, field="logic_check.verdict", old="keep", new="keep",
               cause="relevance_rejudged")
        _event(conn, subject_id=cid, field="status", old="open", new="dropped",
               cause="dropped")
        conn.commit()

        feed = activity.load(conn)

        assert feed.unchanged == 1
        assert [e.detail for e in feed.events] == ["status: open → dropped"]

    def test_a_value_appearing_or_clearing_is_a_change(
        self, conn: sqlite3.Connection
    ) -> None:
        """`None → x` and `x → None` are moves. Only equality is a non-event, and an
        `old is None` comparison that treated them as equal would hide every first write.
        """
        cid = _commitment(conn, "book the flight")
        _event(conn, subject_id=cid, field="due_at", old=None, new="2026-09-01",
               cause="upstream_due_moved")
        _event(conn, subject_id=cid, field="goal_id", old="4", new=None, cause="dropped")
        conn.commit()

        feed = activity.load(conn)

        assert feed.unchanged == 0
        assert {e.detail for e in feed.events} == {
            "due_at set to 2026-09-01",
            "goal_id cleared (was 4)",
        }

    def test_a_whole_row_event_has_no_field_and_says_so(
        self, conn: sqlite3.Connection
    ) -> None:
        cid = _commitment(conn, "return the textbook")
        _event(conn, subject_id=cid, field=None, old=None, new=None, cause="resolved")
        conn.commit()

        assert activity.load(conn).events[0].detail == "the row"


class TestTheSubjectSaysWhatItIs:
    def test_a_commitment_speaks_its_own_words(self, conn: sqlite3.Connection) -> None:
        """Without the join the feed reads "commitment 329 changed", which is a row id
        and not a sentence."""
        cid = _commitment(conn, "Complete CHM 113 Module 1")
        _event(conn, subject_id=cid, field="status", old="open", new="done",
               cause="resolved")
        conn.commit()

        assert activity.load(conn).events[0].title == "Complete CHM 113 Module 1"

    def test_a_fact_is_named_by_lane_and_key_and_links_to_memory(
        self, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO fact (user_id, subject, key, value, source, status, created_at)"
            " VALUES (1, 'housing', 'address', 'Willow Hall 502', 'manual', 'active', ?)",
            (now_iso(),),
        )
        fid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        _event(conn, subject_table="fact", subject_id=fid, field="value",
               old="Old Hall 1", new="Willow Hall 502", cause="fact_superseded")
        conn.commit()

        event = activity.load(conn).events[0]
        assert event.title == "housing/address"
        assert event.href == "/memory"

    def test_an_unjoinable_subject_falls_back_to_table_and_id(
        self, conn: sqlite3.Connection
    ) -> None:
        """A subject table this reader has no join for must still produce a row. The
        alternative is a feed that silently drops the writers wired after it."""
        _event(conn, subject_table="open_question", subject_id=7, field="status",
               old="open", new="answered", cause="answered")
        conn.commit()

        event = activity.load(conn).events[0]
        assert event.title == "open_question 7"
        assert event.href is None


class TestTheThreeStreams:
    def test_all_three_merge_into_one_list_ordered_by_time(
        self, conn: sqlite3.Connection
    ) -> None:
        cid = _commitment(conn, "submit the quiz")
        _event(conn, subject_id=cid, field="status", old="open", new="done",
               cause="resolved", at="2026-08-20T10:00:00+00:00")
        item = _source_item(conn, source="calendar:apple", title="CHM 113 (Lab)")
        conn.execute(
            "INSERT INTO source_item_retraction (source_item_id, user_id, retracted_at,"
            " reason) VALUES (?, 1, ?, ?)",
            (item, "2026-08-20T11:00:00+00:00", "calendar:apple re-read .. and no longer"
             " returns this item"),
        )
        conn.execute(
            "INSERT INTO notification (user_id, kind, subject_key, local_date, title,"
            " body, delivered, created_at)"
            " VALUES (1, 'overdue-today', 'k', '2026-08-20', 'Due today: 5', 'body',"
            " 'osascript', '2026-08-20T12:00:00+00:00')"
        )
        conn.commit()

        feed = activity.load(conn, now=datetime(2026, 8, 21, tzinfo=UTC))

        assert [e.stream for e in feed.events] == ["notice", "retraction", "change"]
        assert feed.totals == {"change": 1, "retraction": 1, "notice": 1}

    def test_a_stream_filter_narrows_and_reports_only_that_stream(
        self, conn: sqlite3.Connection
    ) -> None:
        cid = _commitment(conn, "pay the fee")
        _event(conn, subject_id=cid, field="status", old="open", new="done",
               cause="resolved")
        item = _source_item(conn, source="canvas:ics", title="EC - Flowcharting")
        conn.execute(
            "INSERT INTO source_item_retraction (source_item_id, user_id, retracted_at,"
            " reason) VALUES (?, 1, ?, 'gone')",
            (item, now_iso()),
        )
        conn.commit()

        feed = activity.load(conn, stream="retraction")

        assert [e.stream for e in feed.events] == ["retraction"]
        assert feed.totals == {"retraction": 1}
        assert feed.events[0].href == f"/source/{item}"

    def test_an_unknown_stream_shows_everything_rather_than_nothing(
        self, conn: sqlite3.Connection
    ) -> None:
        """A typo in a query string should show the feed, not an empty page."""
        cid = _commitment(conn, "renew the parking permit")
        _event(conn, subject_id=cid, field="status", old="open", new="done",
               cause="resolved")
        conn.commit()

        feed = activity.load(conn, stream="nonsense")

        assert feed.stream is None
        assert len(feed.events) == 1

    def test_a_failed_delivery_is_distinguishable_from_a_delivered_one(
        self, conn: sqlite3.Connection
    ) -> None:
        """The ledger answers "what did the system tell the owner and when", which
        Notification Center's memory cannot — so a banner that never reached the screen
        must not look identical to one that did."""
        for kind, delivered in (("a", "osascript"), ("b", "failed: no GUI session")):
            conn.execute(
                "INSERT INTO notification (user_id, kind, subject_key, local_date, title,"
                " body, delivered, created_at)"
                " VALUES (1, ?, 'k', '2026-08-20', 'title', 'body', ?, ?)",
                (kind, delivered, now_iso()),
            )
        conn.commit()

        outcomes = {e.cause: e.new for e in activity.load(conn).events}
        assert outcomes == {"a": "osascript", "b": "failed: no GUI session"}


class TestTheRetractionReasonIsReadable:
    """Every retraction reason is the same sentence about a re-read window, and the
    windows are stored to the microsecond. Rendered raw they push the withdrawn item's
    own name off the line."""

    def test_instants_are_shortened_to_dates_and_the_claim_is_untouched(self) -> None:
        trimmed = activity._readable_window(
            "calendar:apple re-read 2026-08-18T20:02:28.745075+00:00 .."
            " 2026-09-15T20:02:28.745075+00:00 in full and no longer returns this item"
        )
        assert trimmed == (
            "calendar:apple re-read 2026-08-18 .. 2026-09-15 in full and no longer"
            " returns this item"
        )

    def test_a_reason_with_no_instants_survives_unchanged(self) -> None:
        """The trim must be a no-op on anything it does not recognise — a connector may
        write any sentence it likes."""
        assert activity._readable_window("gone from the feed") == "gone from the feed"

    def test_the_stored_reason_is_not_rewritten(
        self, conn: sqlite3.Connection
    ) -> None:
        """Presentation only. The evidence stays as the connector wrote it."""
        item = _source_item(conn, source="calendar:apple", title="CHM 113 (Lab)")
        raw = "calendar:apple re-read 2026-08-18T20:02:28.745075+00:00 .. now"
        conn.execute(
            "INSERT INTO source_item_retraction (source_item_id, user_id, retracted_at,"
            " reason) VALUES (?, 1, ?, ?)",
            (item, now_iso(), raw),
        )
        conn.commit()

        assert "2026-08-18T20:02:28" not in activity.load(conn).events[0].detail
        stored = conn.execute(
            "SELECT reason FROM source_item_retraction WHERE source_item_id = ?", (item,)
        ).fetchone()
        assert stored["reason"] == raw


class TestTheWindow:
    def test_events_older_than_the_window_are_not_read(
        self, conn: sqlite3.Connection
    ) -> None:
        cid = _commitment(conn, "old business")
        now = datetime(2026, 8, 25, tzinfo=UTC)
        _event(conn, subject_id=cid, field="status", old="open", new="done",
               cause="resolved", at=(now - timedelta(days=40)).isoformat())
        _event(conn, subject_id=cid, field="status", old="done", new="open",
               cause="reopened", at=(now - timedelta(days=3)).isoformat())
        conn.commit()

        assert len(activity.load(conn, days=30, now=now).events) == 1
        assert len(activity.load(conn, days=90, now=now).events) == 2

    def test_the_limit_is_reported_rather_than_hiding_rows_silently(
        self, conn: sqlite3.Connection
    ) -> None:
        cid = _commitment(conn, "many changes")
        for i in range(5):
            _event(conn, subject_id=cid, field="status", old=str(i), new=str(i + 1),
                   cause="churn")
        conn.commit()

        feed = activity.load(conn, limit=3)

        assert len(feed.events) == 3
        assert feed.truncated is True


class TestThePage:
    def test_it_renders_empty_with_a_declarative_state(self, client: TestClient) -> None:
        page = client.get("/activity")
        assert page.status_code == 200
        body = panel_slice(page.text, "panel-activity")
        assert "Nothing recorded in the last 30 days." in body

    def test_it_lists_a_real_event(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        cid = _commitment(conn, "Complete Bio 181 Lecture Syllabus Quiz")
        _event(conn, subject_id=cid, field="status", old="open", new="dropped",
               cause="dropped")
        conn.commit()

        body = panel_slice(client.get("/activity").text, "panel-activity")
        assert "Complete Bio 181 Lecture Syllabus Quiz" in body
        assert "status: open → dropped" in body

    def test_the_unchanged_count_is_stated_not_dropped(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        cid = _commitment(conn, "quiet commitment")
        _event(conn, subject_id=cid, field="logic_check.verdict", old="keep", new="keep",
               cause="relevance_rejudged")
        conn.commit()

        body = panel_slice(client.get("/activity").text, "panel-activity")
        assert "1 re-judgement" in body
        assert "changed nothing" in body

    def test_an_out_of_range_window_falls_back_instead_of_refusing(
        self, client: TestClient
    ) -> None:
        """This is a nav destination. A page that 422s because a query string was
        hand-edited is worse than one that shows the default."""
        page = client.get("/activity?days=99999&stream=bogus")
        assert page.status_code == 200
        assert "last 30 days" in page.text

    def test_the_page_is_in_the_nav(self, client: TestClient) -> None:
        assert 'href="/activity"' in client.get("/").text
