"""docs/08 D7: "A test asserts that a message with a denylisted address in any recipient
field produces zero rows. This test does not get skipped."

It is not skipped, and it does not depend on the configured runtime mode. The owner has
selected BOUNDARY_MODE=full_scope, under which the boundary allows everything — so these
tests construct an enforcing boundary explicitly. If the mode is ever switched back to
`exclude`, this is the test that says whether the switch works, and it must already be
passing on the day that decision is made rather than being written then.
"""

from __future__ import annotations

import pytest

from backglass.config import Settings
from backglass.connectors.boundary import Boundary, purge
from backglass.db import connect, migrate
from backglass.ledger import Ledger
from tests.conftest import gmail_message, make_connector

DENY_DOMAINS = ["clientexample.gov", "wic-partner.org"]
DENY_ADDRESSES = ["dana.personal@gmail.com"]


@pytest.fixture
def enforcing() -> Boundary:
    return Boundary(mode="exclude", deny_domains=DENY_DOMAINS, deny_addresses=DENY_ADDRESSES)


# ─────────────────────────────────────────────────────────────────── D3, D4


@pytest.mark.parametrize(
    "field",
    ["from", "to", "cc", "bcc"],
    ids=["From", "To", "Cc", "Bcc"],
)
def test_d7_denylisted_address_in_any_recipient_field_produces_zero_rows(
    field: str, enforcing: Boundary, settings: Settings, conn
) -> None:
    """D7. The whole requirement, one field at a time."""
    spec = {
        "id": f"msg-{field}",
        "from": "colleague@example.com",
        "to": "contactdharsan@gmail.com",
        "subject": "Scope for the WIC rollout",
        "date": "Fri, 10 Jul 2026 09:15:00 -0700",
        "body": "I'll send the revised scope by Thursday.",
    }
    spec[field] = "Dana Whitfield <dwhitfield@clientexample.gov>"

    connector = make_connector([gmail_message(spec)], enforcing)
    ledger = Ledger(conn, settings)
    items = list(connector.fetch(None))

    assert items == [], f"a denylisted address in {field} still produced an item"
    for item in items:
        ledger.upsert_source_item(item)

    rows = conn.execute("SELECT COUNT(*) AS n FROM source_item").fetchone()
    assert rows["n"] == 0
    assert ledger.writes == 0
    assert connector.excluded == 1
    assert connector.excluded_by_rule == {"clientexample.gov": 1}


def test_subdomains_are_covered_but_lookalikes_are_not(enforcing: Boundary) -> None:
    """D3: "including subdomains" — and not including domains that merely end the same."""
    assert enforcing.match("a@wic.clientexample.gov") == "clientexample.gov"
    assert enforcing.match("a@deep.nested.clientexample.gov") == "clientexample.gov"
    assert enforcing.match("a@notclientexample.gov") is None
    assert enforcing.match("a@clientexample.gov.attacker.com") is None


def test_matching_is_case_insensitive(enforcing: Boundary) -> None:
    assert enforcing.match("Dana.Personal@GMAIL.com") == "dana.personal@gmail.com"
    assert enforcing.match("X@ClientExample.GOV") == "clientexample.gov"


def test_display_names_containing_commas_do_not_defeat_the_check(enforcing: Boundary) -> None:
    """A naive comma split would read this as two malformed addresses and match neither."""
    spec = {
        "id": "comma",
        "from": "safe@example.com",
        "to": '"Whitfield, Dana" <dwhitfield@clientexample.gov>, other@example.com',
        "subject": "s",
        "date": "Fri, 10 Jul 2026 09:15:00 -0700",
        "body": "b",
    }
    connector = make_connector([gmail_message(spec)], enforcing)
    assert list(connector.fetch(None)) == []


def test_allowed_message_passes_through(enforcing: Boundary) -> None:
    spec = {
        "id": "clean",
        "from": "colleague@example.com",
        "to": "contactdharsan@gmail.com",
        "subject": "Scope",
        "date": "Fri, 10 Jul 2026 09:15:00 -0700",
        "body": "I'll send the revised scope by Thursday.",
    }
    connector = make_connector([gmail_message(spec)], enforcing)
    items = list(connector.fetch(None))
    assert len(items) == 1
    assert connector.excluded == 0


def test_full_scope_mode_allows_everything() -> None:
    """The owner's selected mode. The denylist is inert, deliberately."""
    boundary = Boundary(mode="full_scope", deny_domains=DENY_DOMAINS)
    assert boundary.check(["dwhitfield@clientexample.gov"]).allowed is True
    assert boundary.enforcing is False


# ─────────────────────────────────────────────────────────────────────── D6


def test_purge_removes_previously_stored_items_and_their_commitments(
    settings: Settings, tmp_path
) -> None:
    """D6. "Adding a domain to the denylist triggers a purge of any previously stored
    items matching it, with a report of what was removed."

    The denylist is incomplete on day one. This is the clean way to fix that discovery.
    """
    db = connect(tmp_path / "purge.db")
    migrate(db)

    permissive = Boundary(mode="exclude")  # nothing denied yet
    spec = {
        "id": "later-denied",
        "from": "Dana <dwhitfield@clientexample.gov>",
        "to": "contactdharsan@gmail.com",
        "subject": "Scope",
        "date": "Fri, 10 Jul 2026 09:15:00 -0700",
        "body": "I'll send the revised scope by Thursday.",
    }
    keeper = {**spec, "id": "keeper", "from": "colleague@example.com"}

    ledger = Ledger(db, settings)
    connector = make_connector([gmail_message(spec), gmail_message(keeper)], permissive)
    ids = [ledger.upsert_source_item(item)[0] for item in connector.fetch(None)]
    assert len(ids) == 2
    db.execute(
        "INSERT INTO commitment (user_id, direction, what, confidence, source_item_id, "
        "created_at) "
        "VALUES (1, 'i_owe', 'revised scope', 0.9, ?, '2026-07-10T00:00:00+00:00')",
        (ids[0],),
    )

    # The client is discovered and added to the denylist.
    corrected = Boundary(mode="exclude", deny_domains=["clientexample.gov"])

    dry = purge(db, corrected, dry_run=True)
    assert (dry.source_items, dry.commitments) == (1, 1)
    assert db.execute("SELECT COUNT(*) AS n FROM source_item").fetchone()["n"] == 2

    report = purge(db, corrected)
    assert (report.source_items, report.commitments) == (1, 1)
    assert report.matched_rules == {"clientexample.gov": 1}
    assert db.execute("SELECT COUNT(*) AS n FROM source_item").fetchone()["n"] == 1
    assert db.execute("SELECT COUNT(*) AS n FROM commitment").fetchone()["n"] == 0
    survivor = db.execute("SELECT external_id FROM source_item").fetchone()
    assert survivor["external_id"] == "keeper"
    db.close()


def test_purge_is_a_no_op_when_the_boundary_is_not_enforcing(settings: Settings, conn) -> None:
    report = purge(conn, Boundary.from_settings(settings))
    assert report.total == 0


def test_raw_delete_of_source_items_is_forbidden(conn):  # type: ignore[no-untyped-def]
    """0005: docs/03 'kept forever' is now schema-enforced, not convention."""
    import sqlite3

    import pytest

    conn.execute(
        "INSERT INTO source_item (source, external_id, fetched_at, occurred_at,"
        " content_hash) VALUES ('gmail:personal', 'x', '2026-07-30', '2026-07-30', 'h')"
    )
    with pytest.raises(sqlite3.DatabaseError, match="kept forever"):
        conn.execute("DELETE FROM source_item")
    # And the gate is closed at rest.
    assert conn.execute("SELECT open FROM purge_gate").fetchone()["open"] == 0
