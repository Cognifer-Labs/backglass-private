"""Per-source pause switch. Phase 7.

A disabled source keeps credential, cursor, and every stored item; the sync skips
it; the brief and the failure alert stay quiet about it — the owner chose the
silence, so it is not a failure state.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from backglass.config import Settings
from backglass.connectors import credentials
from backglass.web.app import create_app

TODAY = date(2026, 7, 30)


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


def test_set_enabled_upserts_and_reads_back(conn: sqlite3.Connection) -> None:
    credentials.set_enabled(conn, "imessage", False)
    assert credentials.disabled_sources(conn) == {"imessage"}
    credentials.set_enabled(conn, "imessage", True)
    assert credentials.disabled_sources(conn) == set()
    # Cursor survives a pause/resume cycle.
    credentials.save_cursor(conn, "github", "2026-07-30T00:00:00+00:00")
    credentials.set_enabled(conn, "github", False)
    credentials.set_enabled(conn, "github", True)
    row = conn.execute("SELECT cursor FROM credential WHERE source = 'github'").fetchone()
    assert row["cursor"] == "2026-07-30T00:00:00+00:00"


def test_disabled_source_is_not_a_brief_failure(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    from backglass.brief import daily

    credentials.mark_failed(conn, "gmail:personal", "invalid_grant")
    failing = daily.failure_section(conn, TODAY, settings)
    assert any("gmail:personal" in line.text for line in failing.lines)

    credentials.set_enabled(conn, "gmail:personal", False)
    quiet = daily.failure_section(conn, TODAY, settings)
    assert not any("gmail:personal" in line.text for line in quiet.lines)


def test_toggle_route_flips_state_and_rerenders(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    credentials.save_cursor(conn, "notes", "123.0")
    conn.commit()
    paused = client.post("/sources/notes/disable")
    assert paused.status_code == 200
    assert "Resume" in paused.text
    assert conn.execute(
        "SELECT enabled FROM credential WHERE source = 'notes'"
    ).fetchone()["enabled"] == 0

    resumed = client.post("/sources/notes/enable")
    assert "Pause" in resumed.text
    assert client.post("/sources/notes/sideways").status_code == 422


def test_paused_failing_source_raises_no_sidebar_alert(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    credentials.mark_failed(conn, "gmail:personal", "invalid_grant")
    conn.commit()
    assert "views are incomplete" in client.get("/").text
    credentials.set_enabled(conn, "gmail:personal", False)
    conn.commit()
    assert "views are incomplete" not in client.get("/").text
