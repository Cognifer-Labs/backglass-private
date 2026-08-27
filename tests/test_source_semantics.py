"""Which sources hand back records that move, declared once instead of discovered.

`source_item` is immutable and stays immutable. What this is about is how a differing
`content_hash` is *reported*: a mail body that changes means something is wrong, and a
Canvas due date that changes means Tuesday. Reporting both as errors produced seven
identical failures in every sync forever — the condition is never resolved by anything,
so the error came back every thirty minutes and the Sources panel learned to be ignored.
"""

from __future__ import annotations

import sqlite3

from backglass.config import Settings
from backglass.connectors.base import SNAPSHOT_SOURCES, SourceItem, is_snapshot
from backglass.ledger import Ledger


def _item(source: str, *, body: str) -> SourceItem:
    import hashlib

    return SourceItem(
        source=source,
        external_id="thing-1",
        occurred_at="2026-08-01T09:00:00-07:00",
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
        author="someone@example.com",
        title="A thing",
        body_text=body,
    )


class TestTheDeclaration:
    def test_the_sources_whose_records_move(self) -> None:
        """Pinned, because it is a claim about the world rather than a derived value.
        Each of these has been observed changing."""
        assert frozenset({
            "canvas:ics", "calendar:apple", "calendar:asu", "reminders", "apple-notes",
        }) == SNAPSHOT_SOURCES

    def test_a_message_source_is_not_one_of_them(self) -> None:
        """The absence is the claim: a mail body that changes after the fact is either a
        bug in the extractor or something upstream lying, and both are worth an error."""
        assert is_snapshot("apple-mail") is False
        assert is_snapshot("imessage") is False
        assert is_snapshot("gmail:personal") is False

    def test_a_second_feed_inherits_its_familys_semantics(self) -> None:
        """A third calendar or a second Canvas feed must not silently fall back to
        stream — that fallback is what produced the noise this exists to retire."""
        assert is_snapshot("canvas:ics:2027") is True
        assert is_snapshot("calendar:work") is True


class TestWhatTheLedgerDoesWithIt:
    def _stored(self, conn: sqlite3.Connection, settings: Settings, source: str) -> Ledger:
        ledger = Ledger(conn, settings)
        ledger.upsert_source_item(_item(source, body="the original text"))
        return ledger

    def test_a_changed_canvas_record_is_news_not_an_error(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        ledger = self._stored(conn, settings, "canvas:ics")

        ledger.upsert_source_item(_item("canvas:ics", body="the due date moved"))

        assert ledger.stats.source_item_conflicts == []
        assert ledger.stats.upstream_changes == [("canvas:ics", "thing-1")]

    def test_a_changed_mail_body_is_still_an_error(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The near-miss, and the reason the declaration is narrow: widening it far enough
        to silence the noise would also silence the thing worth hearing."""
        ledger = self._stored(conn, settings, "apple-mail")

        ledger.upsert_source_item(_item("apple-mail", body="something rewrote this"))

        assert ledger.stats.source_item_conflicts == ["apple-mail:thing-1"]
        assert ledger.stats.upstream_changes == []

    def test_the_stored_item_is_never_updated_either_way(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Immutability is not what changed here. `source_item` keeps the words it was
        first read with; the new value belongs to that source's mirror."""
        ledger = self._stored(conn, settings, "canvas:ics")
        conn.commit()

        ledger.upsert_source_item(_item("canvas:ics", body="the due date moved"))
        conn.commit()

        rows = conn.execute("SELECT body_text FROM source_item").fetchall()
        assert [r["body_text"] for r in rows] == ["the original text"]

    def test_an_unchanged_record_is_neither(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        ledger = self._stored(conn, settings, "canvas:ics")

        ledger.upsert_source_item(_item("canvas:ics", body="the original text"))

        assert ledger.stats.upstream_changes == []
        assert ledger.stats.source_item_conflicts == []
