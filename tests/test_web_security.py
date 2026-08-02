"""The two guards in backglass/web/security.py, asserted as behaviour.

Loopback is not an origin boundary: any page in any browser on this Mac can reach
127.0.0.1:8765, and every write endpoint takes an empty body and no token. These tests
exist because that exposure is invisible from the code — nothing looks wrong about a
route until you notice nobody is checking who asked.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID
from backglass.web.app import create_app
from backglass.web.security import is_loopback

LOOPBACK = "http://127.0.0.1:8765"


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn
    return TestClient(create_app(settings), base_url=LOOPBACK)


def _commitment(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " content_hash) VALUES (?, 'gmail:personal', 'sec1', ?, ?, 'h-sec1')",
        (USER_ID, now_iso(), "2026-07-14T09:15:00-07:00"),
    )
    source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, confidence, status,"
        " source_item_id, created_at) VALUES (?, 'i_owe', 'send the deck', 0.9, 'open', ?, ?)",
        (USER_ID, source_id, now_iso()),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _status(conn: sqlite3.Connection, cid: int) -> str:
    row = conn.execute("SELECT status FROM commitment WHERE id = ?", (cid,)).fetchone()
    return str(row["status"])


# ── guard 1: the Host allowlist (DNS rebinding) ───────────────────────────


def test_a_rebound_hostname_is_refused_before_it_can_read_anything(
    client: TestClient,
) -> None:
    """The attack this stops: evil.tld with a short TTL that re-points at 127.0.0.1.

    The browser then treats the dashboard as same-origin with the attacker's page and
    can read every response — commitments, people, memory. Binding to loopback does
    not stop it; only refusing the name the browser actually used does.
    """
    response = client.get("/", headers={"Host": "evil.tld"})
    assert response.status_code == 421
    assert "commitment" not in response.text.lower()


@pytest.mark.parametrize(
    "host", ["127.0.0.1:8765", "localhost:8765", "127.0.0.1", "[::1]:8765"]
)
def test_every_loopback_spelling_is_allowed(client: TestClient, host: str) -> None:
    assert client.get("/", headers={"Host": host}).status_code == 200


def test_the_port_is_not_part_of_the_check(client: TestClient) -> None:
    """--port is the owner's choice; pinning it here would break their own dashboard."""
    assert client.get("/", headers={"Host": "127.0.0.1:9999"}).status_code == 200
    assert is_loopback("localhost:1") and not is_loopback("127.0.0.1.evil.tld")


# ── guard 2: cross-site writes ────────────────────────────────────────────


def test_a_forged_cross_site_post_cannot_resolve_a_commitment(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """A hostile page auto-submitting a form at 127.0.0.1:8765 is the whole threat.

    No cookies and no auth means a forged request is byte-identical to a real click,
    so the only thing distinguishing them is the browser's own account of who asked.
    """
    cid = _commitment(conn)
    response = client.post(
        f"/commitments/{cid}/resolve",
        headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.tld"},
    )
    assert response.status_code == 403
    assert _status(conn, cid) == "open", "the ledger is untouched, not just the response"


def test_a_stale_browser_without_sec_fetch_is_caught_by_origin(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    cid = _commitment(conn)
    response = client.post(
        f"/commitments/{cid}/resolve", headers={"Origin": "https://evil.tld"}
    )
    assert response.status_code == 403
    assert _status(conn, cid) == "open"


def test_the_dashboards_own_htmx_write_still_works(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    cid = _commitment(conn)
    response = client.post(
        f"/commitments/{cid}/resolve",
        headers={
            "Sec-Fetch-Site": "same-origin",
            "Origin": LOOPBACK,
            "HX-Request": "true",
        },
    )
    assert response.status_code == 200
    assert _status(conn, cid) == "done"


def test_a_non_browser_caller_is_not_refused(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """curl and the CLI send neither header, and were never subject to CSRF.

    Refusing them would buy nothing and break scripted use of the same endpoints.
    """
    cid = _commitment(conn)
    assert client.post(f"/commitments/{cid}/resolve").status_code == 200
    assert _status(conn, cid) == "done"


def test_reads_are_not_subject_to_the_write_guard(client: TestClient) -> None:
    """The pixel is loaded by a mail client on another origin — that is its job."""
    assert client.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


# ── response headers ──────────────────────────────────────────────────────


def test_the_dashboard_cannot_be_framed(client: TestClient) -> None:
    """Clickjacking reaches the same one-click destructive buttons (Drop, Merge)."""
    headers = client.get("/").headers
    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_the_csp_floor_blocks_outbound_connections(client: TestClient) -> None:
    """docs/08: nothing leaves the machine. The policy makes that structural."""
    csp = client.get("/").headers["Content-Security-Policy"]
    assert "connect-src 'self'" in csp
    assert "default-src 'self'" in csp
