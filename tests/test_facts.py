"""The personal knowledge base (Phase 12): supersession, retraction, export, page."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from backglass import facts
from backglass.config import Settings
from backglass.web.app import create_app


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
        assert "Willow Hall 502" not in forgotten.text
        assert client.post("/memory/99999/forget").status_code == 404

    def test_nav_carries_memory_on_every_page(self, client: TestClient) -> None:
        for path in ("/", "/goals", "/memory"):
            assert "Memory" in client.get(path).text, path

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
