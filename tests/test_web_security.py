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
from backglass.web.security import LOOPBACK_HOSTS, is_loopback, resolve_allowed

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


# ── the configured extra hosts (DASHBOARD_ALLOWED_HOSTS) ──────────────────
#
# The case these exist for: `tailscale serve --bg 8765` puts tailscaled on the tailnet
# address and forwards to 127.0.0.1:8765, keeping the bind loopback. It forwards the
# original Host, so without the name configured the owner's own phone gets a 421.

TAILNET = "backglass-mac.tail1234.ts.net"


@pytest.fixture
def proxied(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn
    allowed = settings.model_copy(update={"dashboard_allowed_hosts": [TAILNET]})
    return TestClient(create_app(allowed), base_url=f"https://{TAILNET}")


def test_a_configured_host_is_answered_for(proxied: TestClient) -> None:
    assert proxied.get("/", headers={"Host": TAILNET}).status_code == 200


def test_a_configured_host_does_not_admit_every_other_name(proxied: TestClient) -> None:
    """The allowlist is a list, not a switch: guard 1 still refuses everything else.

    A rebinding page is unaffected by the owner naming their own tailnet host, and the
    near-miss below is the shape that would matter — a name that merely ends the same
    way must not pass, because `_hostname` compares whole names, not suffixes.
    """
    assert proxied.get("/", headers={"Host": "evil.tld"}).status_code == 421
    assert proxied.get("/", headers={"Host": f"evil.{TAILNET}"}).status_code == 421
    suffixed = proxied.get("/", headers={"Host": f"{TAILNET}.evil.tld"})
    assert suffixed.status_code == 421


def test_loopback_still_works_alongside_a_configured_host(proxied: TestClient) -> None:
    """Configuring a proxy name must not cost the owner their own 127.0.0.1 tab."""
    assert proxied.get("/", headers={"Host": "127.0.0.1:8765"}).status_code == 200


def test_a_write_from_the_configured_host_is_not_a_cross_site_write(
    proxied: TestClient, conn: sqlite3.Connection
) -> None:
    """Guard 2's Origin fallback has to learn the same name, or writes 403 while reads pass.

    A browser that omits Sec-Fetch-Site (Safari before 16.4, some webviews) sends
    `Origin: https://<tailnet name>` on its own same-origin POST. Compared against
    loopback alone that reads as cross-site, and the owner gets a dashboard whose
    buttons all fail — which looks like a bug, not a refusal.
    """
    cid = _commitment(conn)
    response = proxied.post(
        f"/commitments/{cid}/resolve", headers={"Origin": f"https://{TAILNET}"}
    )
    assert response.status_code == 200
    assert _status(conn, cid) == "done"


def test_a_cross_site_write_is_still_refused_through_the_proxy(
    proxied: TestClient, conn: sqlite3.Connection
) -> None:
    cid = _commitment(conn)
    response = proxied.post(
        f"/commitments/{cid}/resolve",
        headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.tld"},
    )
    assert response.status_code == 403
    assert _status(conn, cid) == "open"


def test_the_default_configuration_is_loopback_and_nothing_else(
    settings: Settings,
) -> None:
    """The guard is only as good as its default. Nobody who does not opt in is exposed."""
    assert settings.dashboard_allowed_hosts == []
    assert resolve_allowed() == LOOPBACK_HOSTS


@pytest.mark.parametrize(
    "configured",
    [f"https://{TAILNET}/", f"{TAILNET}:443", f"  {TAILNET.upper()}  "],
)
def test_a_configured_entry_is_read_as_a_name_however_it_was_pasted(
    configured: str,
) -> None:
    """Copied from a browser bar or off `tailscale serve status`, it means one thing."""
    assert TAILNET in resolve_allowed([configured])


def test_an_empty_entry_cannot_become_the_empty_host(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """`_hostname` returns "" for a missing Host header too, so "" must never be allowed."""
    del conn
    assert resolve_allowed(["", "  ", ":8765"]) == LOOPBACK_HOSTS
    blank = settings.model_copy(update={"dashboard_allowed_hosts": [""]})
    client = TestClient(create_app(blank), base_url=LOOPBACK)
    assert client.get("/", headers={"Host": ""}).status_code == 421


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
