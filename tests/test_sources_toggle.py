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
    failing = daily.failure_section(conn, TODAY)
    assert any("gmail:personal" in line.text for line in failing.lines)

    credentials.set_enabled(conn, "gmail:personal", False)
    quiet = daily.failure_section(conn, TODAY)
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


# ── evidence with no connector behind it ──────────────────────────────────


def _item(conn: sqlite3.Connection, source: str, external_id: str) -> None:
    from backglass.db import now_iso
    from backglass.ledger import USER_ID

    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at, "
        " title, body_text, raw_json, content_hash, triage_verdict) "
        "VALUES (?, ?, ?, ?, '2026-07-30T09:00:00-07:00', 't', 'b', '{}', ?, 'keep')",
        (USER_ID, source, external_id, now_iso(), f"h-{source}-{external_id}"),
    )


def test_a_source_with_items_but_no_credential_is_still_named(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """docs/11 §8: the dangerous failure is a view that looks complete and is not.

    `dashboard_sources` reads FROM credential, so an import script's rows — or a source
    whose credential was deleted — would cite into the brief while appearing nowhere in
    the Sources panel, and no sync would ever refresh them.
    """
    from backglass.web import panels

    _item(conn, "calendar:campus", "campus-f26-hon-171")
    _item(conn, "calendar:campus", "campus-f26-psy-101")
    _item(conn, "manual", "quick-1")
    credentials.save_cursor(conn, "notes", "123.0")
    _item(conn, "notes", "note-1")

    panel = panels.sources_panel(conn, settings)
    unmanaged = {row["source"]: row for row in panel.meta["unmanaged"]}

    assert unmanaged["calendar:campus"]["item_count"] == 2
    assert "manual" in unmanaged
    # `notes` has a credential row, so it belongs to the connector-owned list instead.
    assert "notes" not in unmanaged


def test_unmanaged_sources_raise_no_failure_alarm(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    # Nothing to reconnect and nothing to resume: an import is not a broken connector,
    # so it must not turn the panel's keyline vermilion or fire the sidebar alert.
    _item(conn, "calendar:campus", "campus-f26-hon-171")
    conn.commit()
    body = client.get("/").text
    assert "calendar:campus" in body
    assert "no connector" in body
    assert "views are incomplete" not in body
