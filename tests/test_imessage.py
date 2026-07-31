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
from pathlib import Path
from typing import Any

import pytest

from backglass.connectors import _typedstream
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
    connector = IMessageConnector(db_path=store, boundary=enforcing)
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
    connector = IMessageConnector(db_path=store, boundary=enforcing)
    items = list(connector.fetch(None))
    assert items[0].occurred_at == EXPECTED_ISO


def test_second_magnitude_dates_still_convert(tmp_path: Path, enforcing: Boundary) -> None:
    """Older stores and third-party exports hold seconds, not nanoseconds."""
    store = build_store(
        tmp_path / "old.db",
        [{"rowid": 1, "handle": "a@example.com", "text": "hi", "date": SECONDS_2026_07_10}],
    )
    connector = IMessageConnector(db_path=store, boundary=enforcing)
    items = list(connector.fetch(None))
    assert items[0].occurred_at == EXPECTED_ISO


def test_cursor_advances_and_the_second_run_fetches_nothing(
    store: Path, enforcing: Boundary
) -> None:
    """tasks/lessons.md 2026-07-30: assert the second run *fetches* nothing.

    Zero writes proves nothing — content_hash would produce zero writes even if the
    connector re-read the entire store on every run.
    """
    first = IMessageConnector(db_path=store, boundary=enforcing)
    assert len(list(first.fetch(None))) == 2
    assert first.cursor == "2", "the cursor is the highest ROWID seen"

    second = IMessageConnector(db_path=store, boundary=enforcing)
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
    connector = IMessageConnector(db_path=store, boundary=enforcing)
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
    connector = IMessageConnector(db_path=store, boundary=enforcing)
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
    connector = IMessageConnector(db_path=store, boundary=enforcing)
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
    connector = IMessageConnector(db_path=store, boundary=enforcing)
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
    connector = IMessageConnector(db_path=store, boundary=enforcing)

    assert list(connector.fetch(None)) == []
    assert connector.excluded == 1
    assert connector.excluded_by_rule == {"clientexample.gov": 1}


def test_health_names_full_disk_access_when_the_store_is_missing(
    tmp_path: Path, enforcing: Boundary
) -> None:
    """macOS hides chat.db from unapproved processes, so "missing" usually means TCC."""
    connector = IMessageConnector(db_path=tmp_path / "nope" / "chat.db", boundary=enforcing)
    health = connector.health()

    assert health.ok is False
    assert health.detail is not None
    assert "Full Disk Access" in health.detail


def test_health_is_ok_on_a_readable_store(store: Path, enforcing: Boundary) -> None:
    assert IMessageConnector(db_path=store, boundary=enforcing).health().ok is True
