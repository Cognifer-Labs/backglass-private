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
    return TestClient(create_app(settings))


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
        facts.remember(conn, settings, "identity", "name", "Dharsan Kesavan")
        gone = facts.remember(conn, settings, "identity", "old", "stale claim")
        facts.forget(conn, gone)
        conn.commit()
        doc = facts.export_markdown(conn)
        assert "## identity" in doc
        assert "**name**: Dharsan Kesavan" in doc
        assert "stale claim" not in doc
        assert "(manual ·" in doc  # every line says where it came from

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
