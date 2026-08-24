"""An immutable item whose upstream record was edited, said once instead of forever.

The measured failure (`tasks/audit-2026-08-21.md` §2): five CIS 236 assignments had their
due dates moved on 2026-08-21, `canvas_ics` puts the due date in both hashed fields, and
`upsert_source_item` recorded a conflict for each on every read. The sync exited non-zero
every thirty minutes for two days over five edits nobody needed telling about twice — and
a run that reports failure every single time reports nothing at all.

Two properties, and the second is the whole point:

  * a source that says its upstream record moves gets a *revision*, not a conflict, and
    a source that says nothing keeps the fault it already had;
  * the same edit is announced once. A different edit to the same item is news again.
"""

from __future__ import annotations

import sqlite3

from backglass import sync as sync_mod
from backglass.config import Settings
from backglass.connectors.base import SourceItem
from backglass.ledger import Ledger


def item(*, content_hash: str, mutable: bool, body: str = "b") -> SourceItem:
    return SourceItem(
        source="canvas:ics",
        external_id="assignment:7833000",
        occurred_at="2026-08-23",
        content_hash=content_hash,
        author="CIS236",
        title="1-1-1 - Tech in the 21st Century (12:35)",
        body_text=body,
        mutable_upstream=mutable,
    )


def store(conn: sqlite3.Connection, settings: Settings, first: SourceItem) -> Ledger:
    ledger = Ledger(conn, settings)
    ledger.upsert_source_item(first)
    return ledger


class TestWhichKindOfDifference:
    def test_a_mutable_source_records_a_revision_not_a_conflict(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        store(conn, settings, item(content_hash="h1", mutable=True))
        ledger = Ledger(conn, settings)
        ledger.upsert_source_item(item(content_hash="h2", mutable=True))
        assert ledger.stats.source_item_conflicts == []
        assert [r.observed_hash for r in ledger.stats.source_item_revisions] == ["h2"]

    def test_an_ordinary_source_still_records_a_conflict(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """A Gmail body does not change. Nothing here weakens that check — the flag is
        opt-in and the default is the behaviour that already existed."""
        store(conn, settings, item(content_hash="h1", mutable=False))
        ledger = Ledger(conn, settings)
        ledger.upsert_source_item(item(content_hash="h2", mutable=False))
        assert ledger.stats.source_item_revisions == []
        assert ledger.stats.source_item_conflicts == ["canvas:ics:assignment:7833000"]

    def test_an_unchanged_read_is_neither(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        store(conn, settings, item(content_hash="h1", mutable=True))
        ledger = Ledger(conn, settings)
        ledger.upsert_source_item(item(content_hash="h1", mutable=True))
        assert not ledger.stats.source_item_revisions
        assert not ledger.stats.source_item_conflicts

    def test_the_row_itself_is_never_updated(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """docs/03 and the 0002 trigger are untouched: what changed is what a second hash
        *means*, not whether the capture may be rewritten."""
        store(conn, settings, item(content_hash="h1", mutable=True, body="was"))
        Ledger(conn, settings).upsert_source_item(
            item(content_hash="h2", mutable=True, body="now")
        )
        row = conn.execute("SELECT body_text, content_hash FROM source_item").fetchone()
        assert (row["body_text"], row["content_hash"]) == ("was", "h1")


class TestSaidOnce:
    def revise(
        self, conn: sqlite3.Connection, settings: Settings, new_hash: str, **kwargs: object
    ) -> list[str]:
        ledger = Ledger(conn, settings)
        ledger.upsert_source_item(item(content_hash=new_hash, mutable=True))
        return sync_mod._announce_revisions(
            conn, ledger.stats.source_item_revisions, **kwargs  # type: ignore[arg-type]
        )

    def test_the_first_read_after_an_edit_announces_it(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        store(conn, settings, item(content_hash="h1", mutable=True))
        assert self.revise(conn, settings, "h2") == [
            "canvas:ics:assignment:7833000 was edited upstream"
        ]

    def test_every_read_after_that_is_silent(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The bug, exactly: the same edit re-announced on every sync for two days."""
        store(conn, settings, item(content_hash="h1", mutable=True))
        self.revise(conn, settings, "h2")
        assert self.revise(conn, settings, "h2") == []
        assert self.revise(conn, settings, "h2") == []

    def test_a_second_different_edit_is_news_again(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Dedup is on the observed hash, not on the item. An assignment whose date moves
        twice moved twice, and the second one has not been reported."""
        store(conn, settings, item(content_hash="h1", mutable=True))
        self.revise(conn, settings, "h2")
        assert self.revise(conn, settings, "h3") == [
            "canvas:ics:assignment:7833000 was edited upstream"
        ]

    def test_a_dry_run_names_it_and_records_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        store(conn, settings, item(content_hash="h1", mutable=True))
        assert self.revise(conn, settings, "h2", dry_run=True)
        assert conn.execute("SELECT COUNT(*) AS n FROM claim_event").fetchone()["n"] == 0
        # And therefore still unannounced on the next real read, rather than swallowed.
        assert self.revise(conn, settings, "h2")


def test_a_revision_never_degrades_the_run(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """The saturated-signal half. A conflict belongs in `errors`, which is what makes the
    process exit non-zero (docs/02 §Failure policy); a handled upstream edit must not."""
    report = sync_mod.SyncReport()
    report.upstream_revisions.append("canvas:ics:assignment:7833000 was edited upstream")
    assert report.errors == []
    assert report.degraded is False
