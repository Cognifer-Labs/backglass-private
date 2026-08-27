"""The personal knowledge base (Phase 12): supersession, retraction, export, page."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from backglass import facts
from backglass.config import Settings
from backglass.web.app import create_app
from tests.conftest import panel_slice


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


class TestEngine:
    def test_remember_and_recall(self, conn: sqlite3.Connection, settings: Settings) -> None:
        facts.remember(conn, settings, "housing", "dorm", "Willow Hall 502",
                       note="kept selected dorm over MLSBE block", source="assistant")
        conn.commit()
        [f] = facts.recall(conn, "housing")
        assert f.key == "dorm"
        assert f.value == "Willow Hall 502"
        assert f.source == "assistant"

    def test_same_key_supersedes_and_keeps_history(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        first = facts.remember(conn, settings, "housing", "move_in", "2026-08-05 8am")
        second = facts.remember(conn, settings, "housing", "move_in", "2026-08-09 8am")
        conn.commit()
        [f] = facts.recall(conn, "housing")
        assert f.value == "2026-08-09 8am"
        old = conn.execute("SELECT * FROM fact WHERE id = ?", (first,)).fetchone()
        assert old["status"] == "superseded"
        assert old["superseded_by"] == second
        assert old["value"] == "2026-08-05 8am"  # history intact

    def test_forget_retracts_without_delete(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        fid = facts.remember(conn, settings, "premed", "cycle", "apply June 2029")
        facts.forget(conn, fid)
        conn.commit()
        assert facts.recall(conn) == []
        row = conn.execute("SELECT status FROM fact WHERE id = ?", (fid,)).fetchone()
        assert row["status"] == "retracted"
        with pytest.raises(facts.FactError):
            facts.forget(conn, fid)  # already inactive

    def test_export_carries_only_active_with_provenance(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        facts.remember(conn, settings, "identity", "name", "Alex Rivera")
        gone = facts.remember(conn, settings, "identity", "old", "stale claim")
        facts.forget(conn, gone)
        conn.commit()
        doc = facts.export_markdown(conn)
        assert "## identity" in doc
        assert "**name**: Alex Rivera" in doc
        assert "stale claim" not in doc
        assert "(manual ·" in doc  # every line says where it came from

    def test_export_carries_standing_decisions_and_only_standing_ones(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """An assistant that knows the memory but not the decisions will cheerfully
        suggest the thing the owner already declined."""
        from backglass import decisions

        facts.remember(conn, settings, "identity", "name", "Alex Rivera")
        decisions.record(conn, settings, "Sallie Mae application", "not doing it",
                         reasoning="federal loans cover the year")
        first, _ = decisions.record(conn, settings, "Early Start", "declined")
        superseded, _ = decisions.record(conn, settings, "Early Start", "attending after all")
        revisited, _ = decisions.record(conn, settings, "Dorm swap", "requesting")
        decisions.revisit(conn, revisited)
        conn.commit()

        doc = facts.export_markdown(conn)
        assert "## standing decisions" in doc
        assert "**Sallie Mae application**: not doing it — federal loans cover the year" in doc
        assert "**Early Start**: attending after all" in doc
        assert "declined" not in doc  # the superseded claim does not export
        assert "Dorm swap" not in doc  # retraction withdraws it from the memory
        assert "_(decided " in doc

    def test_decisions_alone_are_still_a_memory(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass import decisions

        decisions.record(conn, settings, "Sallie Mae application", "not doing it")
        conn.commit()
        doc = facts.export_markdown(conn)
        assert "(empty)" not in doc
        assert "## standing decisions" in doc

    def test_rejects_blank_and_unknown_source(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        with pytest.raises(facts.FactError):
            facts.remember(conn, settings, "x", "", "value")
        with pytest.raises(facts.FactError):
            facts.remember(conn, settings, "x", "k", "v", source="wishful")


class TestMemoryPage:
    def test_renders_empty_then_add_then_forget(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        page = client.get("/memory")
        assert page.status_code == 200
        assert "Nothing remembered yet" in page.text

        added = client.post(
            "/memory",
            data={"subject": "housing", "key": "dorm", "value": "Willow Hall 502",
                  "note": "MLSBE thread, May 30"},
        )
        assert added.status_code == 200
        assert "Willow Hall 502" in added.text
        assert "MLSBE thread, May 30" in added.text

        fid = conn.execute("SELECT id FROM fact WHERE status='active'").fetchone()["id"]
        forgotten = client.post(f"/memory/{fid}/forget")
        assert forgotten.status_code == 200
        # Of the listing, not of the page: the state doc's "what changed" section names
        # the retracted value on purpose — that is what makes a retraction legible rather
        # than a row that vanished — so a page-wide assertion here would be testing that
        # the doc does not work.
        assert "Willow Hall 502" not in panel_slice(forgotten.text, "panel-facts")
        assert client.post("/memory/99999/forget").status_code == 404

    def test_nav_carries_memory_on_every_page(self, client: TestClient) -> None:
        for path in ("/", "/goals", "/memory"):
            assert "Memory" in client.get(path).text, path

    def test_a_lane_reaches_its_vault_note_when_a_vault_is_configured(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The page and the vault are two views of one row set; stepping between them is
        the point of exporting at all. Writes happen here, backlinks happen there."""
        facts.remember(conn, settings, "housing", "dorm", "Willow Hall 502", source="manual")
        conn.commit()
        sett = settings.model_copy(update={"vault_export_path": "/tmp/Backglass Vault"})
        configured = TestClient(create_app(sett), base_url="http://127.0.0.1:8765")

        body = configured.get("/memory").text

        assert "obsidian://open?vault=Backglass%20Vault" in body
        assert "file=Facts/housing" in body

    def test_no_vault_means_no_link_to_a_note_that_was_never_written(
        self, client: TestClient, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        facts.remember(conn, settings, "housing", "dorm", "Willow Hall 502", source="manual")
        conn.commit()

        assert "obsidian://" not in client.get("/memory").text

    def test_blank_fact_is_422(self, client: TestClient) -> None:
        response = client.post(
            "/memory", data={"subject": "x", "key": " ", "value": "v", "note": ""}
        )
        assert response.status_code == 422


class TestConfigDrift:
    """The knowledge base restates two values the pipeline actually runs on.

    `identity.emails` against `OWNER_EMAILS`, `identity.timezones` against the two
    timezone settings. They agree on the owner's ledger today, and nothing would have
    noticed if they stopped — the shape of the 2026-08-02 lesson, where an invariant
    CLAUDE.md called settled had been false for eight tables for months because no check
    read it.

    Both branches are exercised, because a check nobody has seen fail is a check nobody
    has seen work.
    """

    def _identity(
        self, conn: sqlite3.Connection, settings: Settings, emails: str, zones: str
    ) -> None:
        facts.remember(conn, settings, "identity", "emails", emails)
        facts.remember(conn, settings, "identity", "timezones", zones)

    def test_agreement_is_silent(self, conn: sqlite3.Connection, settings: Settings) -> None:
        cfg = settings.model_copy(
            update={
                "owner_emails": ["me@example.com", "me@example.edu"],
                "default_tz": "America/Phoenix",
                "alt_tz": "Asia/Kolkata",
            }
        )
        self._identity(
            conn,
            cfg,
            "me@example.com (personal) · me@example.edu (school)",
            "America/Phoenix home · Asia/Kolkata when travelling",
        )
        assert facts.config_drift(conn, cfg) == []

    def test_an_address_the_knowledge_base_has_never_heard_of(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """A configured address missing from the KB means the record of the owner is
        stale — a second mailbox was connected and nobody wrote it down."""
        cfg = settings.model_copy(
            update={"owner_emails": ["me@example.com", "new@example.edu"]}
        )
        self._identity(conn, cfg, "me@example.com (personal)", "America/Phoenix home")
        drift = facts.config_drift(conn, cfg)
        assert any("new@example.edu" in line and "identity.emails does not" in line
                   for line in drift)

    def test_an_address_the_config_has_never_heard_of(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The other direction, and the more dangerous one: mail from an address the
        owner considers theirs is not being recognised as their own."""
        cfg = settings.model_copy(update={"owner_emails": ["me@example.com"]})
        self._identity(conn, cfg, "me@example.com · old@example.org", "America/Phoenix home")
        drift = facts.config_drift(conn, cfg)
        assert any("old@example.org" in line and "OWNER_EMAILS does not" in line
                   for line in drift)

    def test_a_timezone_the_knowledge_base_does_not_mention(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        cfg = settings.model_copy(
            update={"owner_emails": ["me@example.com"],
                    "default_tz": "Europe/Berlin", "alt_tz": None}
        )
        self._identity(conn, cfg, "me@example.com", "America/Phoenix home")
        assert any("Europe/Berlin" in line for line in facts.config_drift(conn, cfg))

    def test_a_knowledge_base_with_no_identity_facts_is_not_drift(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Absence is not disagreement. A ledger whose owner has written nothing down
        yet must not fail a preflight check for it."""
        assert facts.config_drift(conn, settings) == []


class TestProvenanceDoor:
    """`fact.source_item_id` had no writer until 2026-08-07.

    The column existed from the table's first migration, `/source/{id}` already renders
    what a source item produced by reading it, and all twenty of the owner's facts had
    it NULL. Not neglect — `memory set` was the only writer and had no way to pass one.
    A column no door can reach reads as unused, and unused columns get dropped.
    """

    def _item(self, conn: sqlite3.Connection) -> int:
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, author, title, body_text, raw_json, content_hash,"
            " triage_verdict) VALUES (1, 'gmail:t', 'f1', '2026-08-07T00:00:00Z',"
            " '2026-08-07T00:00:00Z', 'reg@example.edu', 'Housing', 'b', '{}', 'hf1',"
            " 'keep')"
        )
        return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def test_a_fact_can_name_the_item_that_taught_it(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        item = self._item(conn)
        fact_id = facts.remember(
            conn, settings, "housing", "hall", "Willow 502", source_item_id=item
        )
        stored = conn.execute(
            "SELECT source_item_id FROM fact WHERE id = ?", (fact_id,)
        ).fetchone()
        assert stored["source_item_id"] == item

    def test_the_source_page_shows_the_fact_it_taught(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The surface that reads this column has been rendering an empty section for
        as long as the column has been unreachable. Drive the real door."""
        item = self._item(conn)
        facts.remember(conn, settings, "housing", "hall", "Willow 502", source_item_id=item)
        client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")
        body = client.get(f"/source/{item}").text
        assert "Willow 502" in body


class TestOwnerContext:
    """The knowledge base reaches triage — knowledge-base plan item B.

    Triage's whole job is telling the owner's own obligations from broadcast noise, and
    it did that knowing nothing about the owner. It could not tell that a university's
    admissions mail is a deadline to a prospective student and marketing to one who has
    already enrolled somewhere, because nothing in the prompt said which they were.
    """

    def test_a_ledger_with_no_facts_sends_the_prompt_it_always_sent(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The property that makes this safe to ship: nobody inherits a behaviour change
        they have no data for. An empty knowledge base renders an empty block, so a
        fresh install's triage prompt is byte-for-byte what v2 sent."""
        assert facts.owner_context(conn) == ""

    def test_facts_render_deterministically(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Same ledger, same block — or a caching backend is defeated by dictionary
        order and every call pays for a prefix it should have been billed once for."""
        facts.remember(conn, settings, "education", "college", "ASU Tempe")
        facts.remember(conn, settings, "identity", "name", "Alex Rivera")
        facts.remember(conn, settings, "education", "major", "Biology")
        first = facts.owner_context(conn)
        assert first == facts.owner_context(conn)
        assert first.index("education/college") < first.index("education/major")
        assert first.index("education/major") < first.index("identity/name")

    def test_the_block_is_bounded(self, conn: sqlite3.Connection, settings: Settings) -> None:
        """Every token spent in triage multiplies across the whole inbox. Twenty facts
        is nothing; five hundred would be a tax on every item forever."""
        for i in range(200):
            facts.remember(conn, settings, "bulk", f"k{i:03d}", "x" * 40)
        assert len(facts.owner_context(conn)) <= facts.OWNER_CONTEXT_CHARS + 60

    def test_a_retracted_fact_leaves_the_prompt(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Forgetting is how the owner corrects what the model is told about them, so it
        has to reach the prompt and not only the Memory page."""
        fact_id = facts.remember(conn, settings, "housing", "hall", "Willow 502")
        assert "Willow 502" in facts.owner_context(conn)
        facts.forget(conn, fact_id)
        assert "Willow 502" not in facts.owner_context(conn)

    def test_the_context_reaches_the_model_through_a_real_sync(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        """Drive the real door: the payload the client is handed, not the helper."""
        from backglass.sync import sync
        from tests.conftest import gmail_message, make_connector
        from tests.test_triage_batch import RecordingModel

        facts.remember(conn, settings, "education", "college", "ASU Tempe, enrolled")
        message = gmail_message(
            {
                "id": "ctx1",
                "from": "admissions@other.example",
                "to": "alex.rivera@example.com",
                "subject": "Your place is waiting",
                "date": "Fri, 07 Aug 2026 09:00:00 -0700",
                "body": "Apply by August 15 to secure your spot.",
            }
        )
        model = RecordingModel(triage={"Apply by": {"keep": False, "reason": "marketing"}})
        sync(conn, settings, [make_connector([message], boundary)], model)
        triage_payloads = [user for tier, _, user in model.users if tier == "triage"]
        assert triage_payloads, "the item reached triage"
        assert any("ASU Tempe, enrolled" in payload for payload in triage_payloads)
        assert any("ABOUT THE OWNER" in payload for payload in triage_payloads)
