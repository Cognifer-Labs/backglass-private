"""iMessage connector. docs/07 §Connectors.

The fixture store is built here rather than checked in: a real chat.db is 60-odd tables
of Apple internals, and the six columns this connector reads are the whole contract. If
Apple adds a column, these tests keep passing and they should — the connector does not
read it either.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from backglass.config import Settings
from backglass.connectors import _typedstream
from backglass.connectors.allowlist import Allowlist
from backglass.connectors.boundary import Boundary
from backglass.connectors.imessage import IMessageConnector

#: 2026-07-10 15:04:05 UTC expressed in Apple's epoch, in seconds.
SECONDS_2026_07_10 = 805_388_645
NANOSECONDS_2026_07_10 = SECONDS_2026_07_10 * 1_000_000_000
EXPECTED_ISO = "2026-07-10T15:04:05+00:00"

DENY = ["clientexample.gov"]


@pytest.fixture
def enforcing() -> Boundary:
    return Boundary(mode="exclude", deny_domains=DENY)


def attributed_body(text: str) -> bytes:
    """A real-shaped `attributedBody` typedstream blob carrying `text`.

    Assembled from the format rather than captured from a store, so the bytes that matter
    are visible: the `streamtyped` header, the class chain ending in the literal class
    name `NSString`, the `+` type code for a length-prefixed byte array, the length (one
    byte under 128, otherwise `\\x81` and a little-endian u16), then UTF-8. Anything after
    that is styling — here an empty attribute run, as a one-attribute message really has.
    """
    body = text.encode("utf-8")
    length = bytes([len(body)]) if len(body) < 0x80 else b"\x81" + struct.pack("<H", len(body))
    return (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01\x40\x84\x84\x84"
        b"\x19NSMutableAttributedString\x00\x84\x84\x12NSAttributedString"
        b"\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString"
        b"\x01\x94\x84\x01+" + length + body + b"\x86\x84\x02\x69\x49\x01\x00"
    )


def test_extract_text_reads_the_nsstring_out_of_a_typedstream_blob() -> None:
    assert _typedstream.extract_text(attributed_body("Deck by Friday?")) == "Deck by Friday?"


def test_extract_text_reads_the_two_byte_length_form() -> None:
    """Anything over 127 bytes switches to `\\x81` plus a little-endian u16."""
    long_text = "Deck by Friday? " * 20
    assert _typedstream.extract_text(attributed_body(long_text)) == long_text.strip()


def test_extract_text_returns_none_rather_than_raising_on_garbage() -> None:
    """A blob we cannot read must degrade to "no text", never to a failed sync."""
    assert _typedstream.extract_text(b"") is None
    assert _typedstream.extract_text(b"\x04\x0bstreamtyped nothing familiar here") is None
    # NSString marker present, but the length runs off the end of the blob.
    assert _typedstream.extract_text(b"\x84NSString\x01\x94\x84\x01+\x40short") is None


def build_store(path: Path, messages: list[dict[str, Any]]) -> Path:
    """A minimal chat.db: only the columns the connector actually joins on.

    `messages` specs are compact — rowid, date, text, is_from_me, handle, chat.
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, display_name TEXT);
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY,
            date INTEGER,
            text TEXT,
            attributedBody BLOB,
            associated_message_type INTEGER DEFAULT 0,
            is_from_me INTEGER,
            handle_id INTEGER
        );
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        """
    )
    handles: dict[str, int] = {}
    chats: dict[str, int] = {}
    for spec in messages:
        handle = spec.get("handle")
        handle_id = 0
        if handle:
            if handle not in handles:
                handles[handle] = len(handles) + 1
                conn.execute(
                    "INSERT INTO handle (ROWID, id) VALUES (?, ?)", (handles[handle], handle)
                )
            handle_id = handles[handle]
        conn.execute(
            "INSERT INTO message"
            " (ROWID, date, text, attributedBody, associated_message_type,"
            "  is_from_me, handle_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                spec["rowid"],
                spec.get("date", NANOSECONDS_2026_07_10),
                spec.get("text"),
                spec.get("attributed_body"),
                int(spec.get("associated_message_type", 0)),
                int(spec.get("is_from_me", 0)),
                handle_id,
            ),
        )
        chat = spec.get("chat")
        if chat is not None:
            if chat not in chats:
                chats[chat] = len(chats) + 1
                conn.execute(
                    "INSERT INTO chat (ROWID, display_name) VALUES (?, ?)",
                    (chats[chat], chat),
                )
            conn.execute(
                "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
                (chats[chat], spec["rowid"]),
            )
    conn.commit()
    conn.close()
    return path


#: The fixture store's group chat and its one-to-one handle. Every connector built in
#: this file allows both, so the allowlist is never what a test is accidentally asserting
#: — the cases that are *about* the allowlist build their own.
FIXTURE_CHATS = Allowlist(
    ("Phoenix build", "+14805551212", "a@example.com", "case@wic.clientexample.gov")
)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return build_store(
        tmp_path / "chat.db",
        [
            {
                "rowid": 1,
                "handle": "+14805551212",
                "text": "Can you send the deck by Friday?",
                "chat": "Phoenix build",
            },
            {"rowid": 2, "handle": "+14805551212", "text": "On it", "is_from_me": 1},
        ],
    )


def test_maps_a_message_onto_a_source_item(store: Path, enforcing: Boundary) -> None:
    connector = IMessageConnector(db_path=store, boundary=enforcing, allowlist=FIXTURE_CHATS)
    items = list(connector.fetch(None))

    assert connector.name == "imessage"
    assert len(items) == 2, "one message, one item — nothing is grouped into threads"

    inbound = items[0]
    assert inbound.source == "imessage"
    assert inbound.external_id == "1"
    assert inbound.author == "+14805551212"
    assert inbound.title == "Phoenix build", "the chat display name wins over the handle"
    assert inbound.body_text == "Can you send the deck by Friday?"
    assert json.loads(inbound.raw_json) == {
        "chat": "Phoenix build",
        "is_from_me": False,
        "handle": "+14805551212",
    }
    assert inbound.content_hash

    outbound = items[1]
    assert outbound.author == "me", "is_from_me flips the author, not the handle"
    assert outbound.title == "+14805551212", "no chat name falls back to the handle"


def test_apple_nanosecond_epoch_converts_exactly(store: Path, enforcing: Boundary) -> None:
    """CLAUDE.md rule 4 resolves every relative date against this value.

    An off-by-31-years conversion — Unix epoch instead of Apple's — would make "by Friday"
    in a text resolve against 1995, so the exact string is asserted rather than a range.
    """
    connector = IMessageConnector(db_path=store, boundary=enforcing, allowlist=FIXTURE_CHATS)
    items = list(connector.fetch(None))
    assert items[0].occurred_at == EXPECTED_ISO


def test_second_magnitude_dates_still_convert(tmp_path: Path, enforcing: Boundary) -> None:
    """Older stores and third-party exports hold seconds, not nanoseconds."""
    store = build_store(
        tmp_path / "old.db",
        [{"rowid": 1, "handle": "a@example.com", "text": "hi", "date": SECONDS_2026_07_10}],
    )
    connector = IMessageConnector(db_path=store, boundary=enforcing, allowlist=FIXTURE_CHATS)
    items = list(connector.fetch(None))
    assert items[0].occurred_at == EXPECTED_ISO


def test_cursor_advances_and_the_second_run_fetches_nothing(
    store: Path, enforcing: Boundary
) -> None:
    """tasks/lessons.md 2026-07-30: assert the second run *fetches* nothing.

    Zero writes proves nothing — content_hash would produce zero writes even if the
    connector re-read the entire store on every run.
    """
    first = IMessageConnector(db_path=store, boundary=enforcing, allowlist=FIXTURE_CHATS)
    assert len(list(first.fetch(None))) == 2
    assert first.cursor == "2", "the cursor is the highest ROWID seen"

    second = IMessageConnector(db_path=store, boundary=enforcing, allowlist=FIXTURE_CHATS)
    assert list(second.fetch(first.cursor)) == [], "already-seen messages are not re-read"
    assert second.cursor == "2", "an empty run holds the watermark rather than resetting it"


def test_a_null_text_row_recovers_its_body_from_attributedbody(
    tmp_path: Path, enforcing: Boundary
) -> None:
    """docs/12 §1. Post-Ventura most rows look like this, and `SELECT text` drops them."""
    store = build_store(
        tmp_path / "chat.db",
        [
            {
                "rowid": 1,
                "handle": "+14805551212",
                "text": None,
                "attributed_body": attributed_body("Deck by Friday?"),
            }
        ],
    )
    connector = IMessageConnector(db_path=store, boundary=enforcing, allowlist=FIXTURE_CHATS)
    items = list(connector.fetch(None))

    assert [item.body_text for item in items] == ["Deck by Friday?"]


def test_an_undecodable_blob_skips_the_row_without_raising(
    tmp_path: Path, enforcing: Boundary
) -> None:
    """A failing blob degrades to a textless row — CLAUDE.md rule 5, at row scale."""
    store = build_store(
        tmp_path / "chat.db",
        [
            {
                "rowid": 1,
                "handle": "+14805551212",
                "text": None,
                "attributed_body": b"\xff\x00",
            },
            {"rowid": 2, "handle": "+14805551212", "text": "still here"},
        ],
    )
    connector = IMessageConnector(db_path=store, boundary=enforcing, allowlist=FIXTURE_CHATS)
    items = list(connector.fetch(None))

    assert [item.external_id for item in items] == ["2"]
    assert connector.cursor == "2"


def test_a_tapback_is_skipped_but_still_moves_the_watermark(
    tmp_path: Path, enforcing: Boundary
) -> None:
    """A reaction is not a message. Its text reads like one, which is the whole problem."""
    store = build_store(
        tmp_path / "chat.db",
        [
            {"rowid": 1, "handle": "+14805551212", "text": "Deck by Friday?"},
            {
                "rowid": 2,
                "handle": "+14805551212",
                "text": "Loved “Deck by Friday?”",
                "associated_message_type": 2000,
                "is_from_me": 1,
            },
            {
                "rowid": 3,
                "handle": "+14805551212",
                "text": "Removed a heart from “Deck by Friday?”",
                "associated_message_type": 3000,
                "is_from_me": 1,
            },
        ],
    )
    connector = IMessageConnector(db_path=store, boundary=enforcing, allowlist=FIXTURE_CHATS)
    items = list(connector.fetch(None))

    assert [item.external_id for item in items] == ["1"]
    assert connector.cursor == "3", "skipped tapbacks still advance the cursor"


def test_null_text_rows_are_skipped_but_still_move_the_watermark(
    tmp_path: Path, enforcing: Boundary
) -> None:
    """A row with neither text nor a decodable blob has nothing worth a model call.

    It still advances the cursor: a skipped row that never moved the watermark would be
    re-read on every run for the life of the store.
    """
    store = build_store(
        tmp_path / "chat.db",
        [
            {"rowid": 1, "handle": "+14805551212", "text": "real text"},
            {"rowid": 2, "handle": "+14805551212", "text": None},
            {"rowid": 3, "handle": "+14805551212", "text": "   "},
            {"rowid": 4, "handle": None, "text": "someone was added to the chat"},
        ],
    )
    connector = IMessageConnector(db_path=store, boundary=enforcing, allowlist=FIXTURE_CHATS)
    items = list(connector.fetch(None))

    assert [item.external_id for item in items] == ["1"]
    assert connector.cursor == "4"


def test_a_denylisted_handle_yields_nothing_and_is_counted(
    tmp_path: Path, enforcing: Boundary
) -> None:
    """D1/D4/D5. The handle is an address, so the boundary applies to it as one."""
    store = build_store(
        tmp_path / "chat.db",
        [{"rowid": 1, "handle": "case@wic.clientexample.gov", "text": "case notes"}],
    )
    connector = IMessageConnector(db_path=store, boundary=enforcing, allowlist=FIXTURE_CHATS)

    assert list(connector.fetch(None)) == []
    assert connector.excluded == 1
    assert connector.excluded_by_rule == {"clientexample.gov": 1}


def test_health_names_full_disk_access_when_the_store_is_missing(
    tmp_path: Path, enforcing: Boundary
) -> None:
    """macOS hides chat.db from unapproved processes, so "missing" usually means TCC."""
    connector = IMessageConnector(
        db_path=tmp_path / "nope" / "chat.db",
        boundary=enforcing,
        allowlist=FIXTURE_CHATS,
    )
    health = connector.health()

    assert health.ok is False
    assert health.detail is not None
    assert "Full Disk Access" in health.detail


def test_health_is_ok_on_a_readable_store(store: Path, enforcing: Boundary) -> None:
    assert IMessageConnector(
        db_path=store, boundary=enforcing,
        allowlist=FIXTURE_CHATS).health().ok is True


def test_wal_only_rows_are_visible(store: Path, enforcing: Boundary) -> None:
    """The verifier's refutation of immutable=1, ported from anki/avorio: the real
    chat.db is WAL, and an immutable open misses rows living only in the -wal file.
    mode=ro must see them while Messages.app still holds the database open."""
    writer = sqlite3.connect(store)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute(
        "INSERT INTO message (ROWID, date, text, is_from_me, handle_id)"
        " VALUES (3, ?, 'Sent the deck', 0, 1)",
        (NANOSECONDS_2026_07_10,),
    )
    writer.commit()  # committed, but sitting in the -wal, not the main file
    try:
        connector = IMessageConnector(
        db_path=store, boundary=enforcing,
        allowlist=FIXTURE_CHATS)
        items = list(connector.fetch(None))
        assert [item.external_id for item in items] == ["1", "2", "3"]
        assert connector.cursor == "3"
    finally:
        writer.close()


# ── the allowlist and the window ────────────────────────────────────────────


def test_a_chat_the_owner_did_not_name_is_not_read(tmp_path: Path, enforcing: Boundary) -> None:
    """An inbox-wide read of a personal message store is the wrong default: most of what
    is in there is other people's words about things the owner never meant to file."""
    store = build_store(
        tmp_path / "mixed.db",
        [
            {"rowid": 1, "handle": "+1555", "text": "dinner Friday?", "chat": "Phoenix build"},
            {"rowid": 2, "handle": "+1999", "text": "unrelated", "chat": "Some other group"},
        ],
    )
    connector = IMessageConnector(
        db_path=store, boundary=enforcing, allowlist=Allowlist(("Phoenix build",))
    )

    items = list(connector.fetch(None))

    assert [i.body_text for i in items] == ["dinner Friday?"]
    assert connector.excluded == 1
    assert connector.excluded_by_rule == {"allowlist": 1}


def test_a_one_to_one_is_named_by_its_handle(tmp_path: Path, enforcing: Boundary) -> None:
    """A one-to-one thread has no display name in the Messages store, so the allowlist
    entry has to be the phone number or address — which is what `imessage chats` prints."""
    store = build_store(
        tmp_path / "dm.db",
        [
            {"rowid": 1, "handle": "+14805551212", "text": "coffee?"},
            {"rowid": 2, "handle": "+19995550000", "text": "spam"},
        ],
    )
    connector = IMessageConnector(
        db_path=store, boundary=enforcing, allowlist=Allowlist(("+14805551212",))
    )

    assert [i.body_text for i in connector.fetch(None)] == ["coffee?"]


def test_an_empty_allowlist_is_unhealthy_rather_than_reading_everything(
    store: Path, enforcing: Boundary
) -> None:
    """The safe direction. A connector that silently reads the whole store because nobody
    has chosen anything is the failure this default exists to prevent.

    Unhealthy but not inert: `sync` fetches without consulting `health()`, so the
    connector still discovers conversations while none are monitored. Without that the
    page would have nothing to offer and there would be no way to bootstrap.
    """
    health = IMessageConnector(db_path=store, boundary=enforcing).health()
    assert health.ok is False
    assert "/chats" in (health.detail or "")


def test_the_watermark_advances_past_rejected_messages(
    tmp_path: Path, enforcing: Boundary
) -> None:
    """A rejection is a settled decision. Leaving the cursor behind those rows would make
    every later run re-read and re-reject the same messages forever."""
    store = build_store(
        tmp_path / "adv.db",
        [
            {"rowid": 7, "handle": "+1555", "text": "kept", "chat": "Phoenix build"},
            {"rowid": 9, "handle": "+1999", "text": "dropped", "chat": "Other"},
        ],
    )
    connector = IMessageConnector(
        db_path=store, boundary=enforcing, allowlist=Allowlist(("Phoenix build",))
    )

    list(connector.fetch(None))

    assert connector.cursor == "9"


def test_a_message_older_than_the_window_is_not_read(
    tmp_path: Path, enforcing: Boundary
) -> None:
    """The cursor stops the connector re-reading what it has seen; the window stops the
    FIRST run reaching back over an entire archive."""
    old = NANOSECONDS_2026_07_10 - (400 * 86_400 * 1_000_000_000)
    store = build_store(
        tmp_path / "old-window.db",
        [
            {"rowid": 1, "handle": "+1555", "text": "ancient", "chat": "Phoenix build",
             "date": old},
            {"rowid": 2, "handle": "+1555", "text": "recent", "chat": "Phoenix build"},
        ],
    )
    connector = IMessageConnector(
        db_path=store,
        boundary=enforcing,
        allowlist=Allowlist(("Phoenix build",)),
        lookback_days=3650,
    )
    assert len(list(connector.fetch(None))) == 2, "a wide window keeps both"

    narrow = IMessageConnector(
        db_path=store, boundary=enforcing, allowlist=Allowlist(("Phoenix build",)),
        lookback_days=90,
    )
    assert [i.body_text for i in narrow.fetch(None)] == ["recent"]


def test_the_window_respects_second_magnitude_timestamps(
    tmp_path: Path, enforcing: Boundary
) -> None:
    """The bug this test was written for: the window compared the raw column against a
    nanosecond threshold, so every legacy seconds-magnitude row fell outside it and the
    archive read as empty. Older stores and third-party exports hold seconds."""
    store = build_store(
        tmp_path / "seconds.db",
        [{"rowid": 1, "handle": "+1555", "text": "hi", "chat": "Phoenix build",
          "date": SECONDS_2026_07_10}],
    )
    connector = IMessageConnector(
        db_path=store, boundary=enforcing, allowlist=Allowlist(("Phoenix build",)),
        lookback_days=3650,
    )
    assert [i.body_text for i in connector.fetch(None)] == ["hi"]


# ── narrowing what is already stored ────────────────────────────────────────


def _stored(conn, *, chat: str, handle: str, occurred_at: str, n: int) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, raw_json, content_hash)"
        " VALUES (1, 'imessage', ?, ?, ?, 'msg', 'body', ?, ?)",
        (f"m{n}", occurred_at, occurred_at,
         json.dumps({"chat": chat, "handle": handle, "is_from_me": False}), f"h{n}"),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


class TestPrune:
    def test_it_removes_what_the_allowlist_no_longer_admits(self, conn) -> None:  # type: ignore[no-untyped-def]
        """A narrowed allowlist has to reach backwards. Otherwise the setting is a promise
        about messages not yet read, and everything captured under the looser rule stays."""
        from backglass.connectors.imessage import prune

        today = date(2026, 8, 3)
        _stored(conn, chat="Phoenix build", handle="+1555", occurred_at="2026-08-01", n=1)
        _stored(conn, chat="Some other group", handle="+1999", occurred_at="2026-08-01", n=2)

        report = prune(
            conn, Allowlist(("Phoenix build",)), lookback_days=90, today=today, dry_run=False
        )

        assert (report.removed, report.kept) == (1, 1)
        rows = conn.execute("SELECT raw_json FROM source_item").fetchall()
        assert all("Phoenix build" in r["raw_json"] for r in rows)

    def test_it_removes_what_falls_outside_the_window(self, conn) -> None:  # type: ignore[no-untyped-def]
        from backglass.connectors.imessage import prune

        today = date(2026, 8, 3)
        _stored(conn, chat="Phoenix build", handle="+1555", occurred_at="2026-08-01", n=1)
        _stored(conn, chat="Phoenix build", handle="+1555", occurred_at="2024-01-01", n=2)

        report = prune(
            conn, Allowlist(("Phoenix build",)), lookback_days=90, today=today, dry_run=False
        )

        assert (report.removed, report.kept) == (1, 1)

    def test_a_dry_run_deletes_nothing(self, conn) -> None:  # type: ignore[no-untyped-def]
        """Deleting someone's messages on the strength of a config value they just typed
        should require saying so twice."""
        from backglass.connectors.imessage import prune

        _stored(conn, chat="Unwanted", handle="+1999", occurred_at="2026-08-01", n=1)

        report = prune(conn, Allowlist(("Phoenix build",)), lookback_days=90,
                       today=date(2026, 8, 3))

        assert report.removed == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM source_item").fetchone()["n"] == 1

    def test_derived_rows_go_with_their_source(self, conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
        """source_item has NOT NULL children with no cascade, so the foreign key would
        reject the parent delete if a dependent were missed."""
        from backglass.connectors.imessage import prune
        from backglass.ledger import Ledger

        source_id = _stored(conn, chat="Unwanted", handle="+1999",
                            occurred_at="2026-08-01", n=1)
        ledger = Ledger(conn, settings)
        ledger.insert_commitment(
            direction="i_owe", entity_id=None, what="thing", due_at=None,
            estimated_minutes=None, estimate_source=None, confidence=0.9,
            source_item_id=source_id, evidence="said so",
        )

        prune(conn, Allowlist(("Phoenix build",)), lookback_days=90,
              today=date(2026, 8, 3), dry_run=False)

        for table in ("source_item", "commitment", "commitment_evidence"):
            n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            assert n == 0, table

    def test_the_delete_gate_is_closed_again_afterwards(self, conn) -> None:  # type: ignore[no-untyped-def]
        """Migration 0005 forbids deleting a source_item except through this gate. Leaving
        it open would silently disarm the retain-forever rule for everything after."""
        from backglass.connectors.imessage import prune

        _stored(conn, chat="Unwanted", handle="+1999", occurred_at="2026-08-01", n=1)
        prune(conn, Allowlist(("Keep",)), lookback_days=90, today=date(2026, 8, 3),
              dry_run=False)

        assert conn.execute("SELECT open FROM purge_gate WHERE id = 1").fetchone()["open"] == 0

    def test_every_table_that_names_a_source_item_is_covered(self, conn) -> None:  # type: ignore[no-untyped-def]
        """Derived from the live schema, not remembered. A table added later and left out
        of DEPENDENTS makes the prune fail its foreign key; this says so in CI instead."""
        from backglass.connectors.imessage import DEPENDENTS

        referencing = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            for fk in conn.execute(f"PRAGMA foreign_key_list({row['name']})")
            if str(fk["table"]) == "source_item"
        }
        # roadmap references a source_item for its interview, which is not derived from a
        # message and never carries one for an iMessage row.
        assert referencing - set(DEPENDENTS) == {"roadmap"}, referencing
