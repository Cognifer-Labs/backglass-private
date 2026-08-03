"""Sentence-level provenance: the quote is stored, restatements are kept, and neither
turns a re-read into a write.

The model has always returned `evidence` — "the exact sentence, verbatim" per
extract/schemas.py — and the write layer dropped it, so the review queue rendered the
email subject under a comment quoting docs/11 §4's "the exact source sentence beneath it
in quotes". These tests are about the sentence surviving the trip from the model response
to the page the owner reads it on.

Every case drives a real door: the extraction path, the dashboard's quick-add form, or an
HTTP GET. The 2026-08-02 lesson — a guard written at one call site while the dashboard's
own write path walked past it, three times — is why none of these call `record_evidence`
directly to prove the rule.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from backglass.config import Settings
from backglass.db import now_iso
from backglass.extract.commitments import apply
from backglass.extract.schemas import CommitmentExtraction
from backglass.ledger import Ledger
from backglass.web.app import create_app
from tests.conftest import healthy_run, panel_slice

OCCURRED = "2026-07-10T09:15:00-07:00"

QUOTE = "I'll send the housing deposit receipt by Friday."
RESTATED = "Still owe you that housing deposit receipt — Friday at the latest."


def an_item(conn: sqlite3.Connection, external_id: str, body: str) -> int:
    """One immutable source item, the way a connector would leave it."""
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, content_hash, triage_verdict) "
        "VALUES (1, 'apple-notes', ?, ?, ?, 'Dana Whitfield <dana@example.gov>',"
        " 'Housing', ?, ?, 'keep')",
        (external_id, now_iso(), OCCURRED, body, external_id),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def an_extraction(quote: str, *, confidence: float = 0.9) -> CommitmentExtraction:
    return CommitmentExtraction.model_validate(
        {
            "commitments": [
                {
                    "direction": "i_owe",
                    "counterparty": "Dana Whitfield <dana@example.gov>",
                    "what": "Send the housing deposit receipt",
                    "due_at": "2026-07-17",
                    "due_is_explicit": True,
                    "confidence": confidence,
                    "evidence": quote,
                }
            ]
        }
    )


def citations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return list(
        conn.execute(
            "SELECT ce.commitment_id, ce.source_item_id, ce.quote, ce.kind "
            "FROM commitment_evidence ce ORDER BY ce.id"
        )
    )


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn  # the app opens its own connections against the same migrated file
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


class TestTheSentenceIsStored:
    def test_extraction_records_the_model_sentence(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        ledger = Ledger(conn, settings)
        item_id = an_item(conn, "note-1", QUOTE)
        apply(
            an_extraction(QUOTE),
            source_item_id=item_id,
            occurred_at=OCCURRED,
            ledger=ledger,
            settings=settings,
        )
        rows = citations(conn)
        assert [(r["quote"], r["kind"], r["source_item_id"]) for r in rows] == [
            (QUOTE, "original", item_id)
        ]

    def test_a_quick_add_cites_the_owners_own_words(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """The dashboard's write path, not the extraction one. Both doors, or neither."""
        response = client.post(
            "/commitments/quick-add",
            data={"what": "Return the lab keys", "direction": "i_owe", "counterparty": ""},
        )
        assert response.status_code == 200
        rows = citations(conn)
        assert len(rows) == 1
        assert rows[0]["kind"] == "manual"
        assert rows[0]["quote"] == "Return the lab keys"


class TestRestatements:
    def test_a_restatement_cites_the_commitment_it_matched(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The dedup pass drops the row and used to drop the sentence with it.

        A thread that repeats a promise four times left one citation and no way to see
        that it had been said again since.
        """
        ledger = Ledger(conn, settings)
        first = an_item(conn, "note-1", QUOTE)
        apply(
            an_extraction(QUOTE),
            source_item_id=first,
            occurred_at=OCCURRED,
            ledger=ledger,
            settings=settings,
        )
        second = an_item(conn, "note-2", RESTATED)
        report = apply(
            an_extraction(RESTATED),
            source_item_id=second,
            occurred_at=OCCURRED,
            ledger=ledger,
            settings=settings,
        )

        assert report.inserted == 0 and report.deduped == 1, "still one commitment"
        assert conn.execute("SELECT COUNT(*) AS n FROM commitment").fetchone()["n"] == 1

        rows = citations(conn)
        assert [(r["kind"], r["source_item_id"], r["quote"]) for r in rows] == [
            ("original", first, QUOTE),
            ("restated", second, RESTATED),
        ]
        assert len({r["commitment_id"] for r in rows}) == 1, "both cite the same row"

    def test_re_reading_the_same_item_writes_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """CLAUDE.md rule 3, at the citation level.

        Extraction is re-run against a better prompt routinely (docs/02 §Immutable source
        items), and each re-run hands the same sentence back for the same item. Without
        the (commitment, source_item) uniqueness that would append a citation every time
        — a growing pile of identical quotes that also reports itself as work done.
        """
        ledger = Ledger(conn, settings)
        item_id = an_item(conn, "note-1", QUOTE)
        apply(
            an_extraction(QUOTE),
            source_item_id=item_id,
            occurred_at=OCCURRED,
            ledger=ledger,
            settings=settings,
        )

        second = Ledger(conn, settings)
        apply(
            an_extraction(QUOTE),
            source_item_id=item_id,
            occurred_at=OCCURRED,
            ledger=second,
            settings=settings,
        )
        assert second.writes == 0, f"the re-read wrote {second.writes} times"
        assert len(citations(conn)) == 1

    def test_a_different_sentence_from_the_same_item_does_not_overwrite_the_first(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The owner has already read the first quote on the board. A later run that
        picks a different sentence out of the same document must not rewrite it under
        them — docs/03's immutability rule, applied to the citation."""
        ledger = Ledger(conn, settings)
        item_id = an_item(conn, "note-1", QUOTE)
        apply(
            an_extraction(QUOTE),
            source_item_id=item_id,
            occurred_at=OCCURRED,
            ledger=ledger,
            settings=settings,
        )
        apply(
            an_extraction("A different sentence entirely."),
            source_item_id=item_id,
            occurred_at=OCCURRED,
            ledger=Ledger(conn, settings),
            settings=settings,
        )
        assert [r["quote"] for r in citations(conn)] == [QUOTE]


class TestTheQueueShowsTheSentence:
    def test_the_review_panel_quotes_the_source_not_the_subject(
        self, client: TestClient, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """docs/11 §4 step 2, driven through the page rather than the query.

        The panel used to print `source_title` here, so this asserted-nothing case is the
        one that catches a regression to it: the subject is 'Housing', the sentence is
        the promise, and only one of them settles whether to accept.
        """
        healthy_run(conn)
        ledger = Ledger(conn, settings)
        item_id = an_item(conn, "note-1", QUOTE)
        apply(
            an_extraction(QUOTE, confidence=0.4),  # below the threshold: the queue
            source_item_id=item_id,
            occurred_at=OCCURRED,
            ledger=ledger,
            settings=settings,
        )

        panel = panel_slice(client.get("/").text, "panel-review")
        # Compared against the escaped form because the sentence is rendered, not
        # printed: the apostrophe in it leaves Jinja as &#39;, and asserting on the raw
        # string would fail for a reason that has nothing to do with provenance.
        assert str(escape(QUOTE)) in panel, "the review queue must show the model sentence"
        assert f'href="/source/{item_id}"' in panel, "and link to the document it is in"

    def test_a_row_with_no_stored_sentence_says_so_rather_than_faking_one(
        self, client: TestClient, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Commitments extracted before migration 0013 have the document but not the
        sentence. Falling back to the subject line would put a non-quote in quotation
        marks, which is the exact confusion this change removes."""
        healthy_run(conn)
        item_id = an_item(conn, "note-old", QUOTE)
        conn.execute(
            "INSERT INTO commitment (user_id, direction, what, confidence, status, "
            " source_item_id, created_at) "
            "VALUES (1, 'i_owe', 'Something from before', 0.4, 'open', ?, ?)",
            (item_id, now_iso()),
        )
        commitment_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO commitment_evidence "
            "(user_id, commitment_id, source_item_id, quote, kind, seen_at) "
            "VALUES (1, ?, ?, NULL, 'original', ?)",
            (commitment_id, item_id, now_iso()),
        )

        panel = panel_slice(client.get("/").text, "panel-review")
        assert "Something from before" in panel
        assert "“" not in panel, "nothing is presented as a quotation when none was stored"
