"""Mail.app's local store. docs/07 §Connectors.

The fixture store is built here rather than checked in, for the same reason the iMessage
one is: a real Mail store is a 189MB index of Apple internals and the three columns this
connector selects on are the whole contract. What is checked is the part that is genuinely
Apple's format — the .emlx wrapper — and the four things a source ships with: a
fixture-backed fetch, idempotency, health both ways, and the boundary.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest

from backglass.connectors.apple_mail import (
    AppleMailConnector,
    body_text,
    mailbox_name,
    parse_emlx,
)
from backglass.connectors.boundary import Boundary

DENY = ["clientexample.gov"]
SENT_AT = "Tue, 28 Jul 2026 09:30:00 -0700"
SENT_ISO = "2026-07-28T09:30:00-07:00"


@pytest.fixture
def enforcing() -> Boundary:
    return Boundary(mode="exclude", deny_domains=DENY)


@pytest.fixture
def boundary() -> Boundary:
    return Boundary(mode="exclude")


def emlx_bytes(
    *,
    sender: str = "Prof Sheppard <sheppard@asu.edu>",
    to: str = "dkesava2@asu.edu",
    cc: str = "",
    subject: str = "MLSBE paperwork",
    body: str = "Please send the signed form by Friday.",
    date: str = SENT_AT,
    message_id: str = "<abc-123@asu.edu>",
) -> bytes:
    """A real-shaped .emlx: byte count, newline, RFC822, then Apple's plist."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    if cc:
        message["Cc"] = cc
    message["Subject"] = subject
    message["Date"] = date
    message["Message-ID"] = message_id
    message.set_content(body)
    raw = message.as_bytes()
    plist = (
        b'<?xml version="1.0"?>'
        b"<plist><dict><key>flags</key><integer>1</integer></dict></plist>"
    )
    return str(len(raw)).encode() + b"\n" + raw + b"\n" + plist


def emlx_with_delivered_to(account: str, message_id: str = "<abc-123@asu.edu>") -> bytes:
    """An .emlx carrying the `Delivered-To` header IMAP delivery adds."""
    raw = emlx_bytes(message_id=message_id)
    head, _, rest = raw.partition(b"\n")
    body = rest[: int(head)]
    extra = f"Delivered-To: {account}\n".encode()
    return str(len(extra) + len(body)).encode() + b"\n" + extra + body + b"\n"


def build_store(root: Path, messages: list[dict]) -> Path:
    """A V10-shaped store: an Envelope Index plus the .emlx files it points at."""
    version = root / "V10"
    data = version / "MailData"
    data.mkdir(parents=True)
    index = data / "Envelope Index"

    conn = sqlite3.connect(index)
    conn.executescript(
        """
        CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
        CREATE TABLE messages (
            ROWID INTEGER PRIMARY KEY,
            date_received INTEGER,
            mailbox INTEGER,
            deleted INTEGER DEFAULT 0
        );
        """
    )
    boxes: dict[str, int] = {}
    for spec in messages:
        url = spec.get("mailbox", "imap://ACCOUNT-A/INBOX")
        if url not in boxes:
            boxes[url] = len(boxes) + 1
            conn.execute(
                "INSERT INTO mailboxes (ROWID, url) VALUES (?, ?)", (boxes[url], url)
            )
        conn.execute(
            "INSERT INTO messages (ROWID, date_received, mailbox, deleted)"
            " VALUES (?, ?, ?, ?)",
            (
                spec["rowid"],
                spec.get("date_received", int(datetime.now(UTC).timestamp())),
                boxes[url],
                spec.get("deleted", 0),
            ),
        )
        if spec.get("no_file"):
            continue
        folder = version / "ACCOUNT-A" / "box.mbox" / "Data" / "Messages"
        folder.mkdir(parents=True, exist_ok=True)
        suffix = ".partial.emlx" if spec.get("partial") else ".emlx"
        name = f"{spec['rowid']}{suffix}"
        (folder / name).write_bytes(
            spec.get("raw") or emlx_bytes(**spec.get("message", {}))
        )
    conn.commit()
    conn.close()
    return root


def connector(root: Path, boundary: Boundary, **kwargs) -> AppleMailConnector:
    return AppleMailConnector(mail_root=root, boundary=boundary, **kwargs)


NOW = int(datetime.now(UTC).timestamp())


class TestTheEmlxWrapper:
    def test_the_message_is_sliced_by_the_declared_count(self) -> None:
        message = parse_emlx(emlx_bytes(body="hello"))
        assert message is not None
        assert message["Subject"] == "MLSBE paperwork"
        assert "hello" in body_text(message)

    def test_a_body_containing_the_plist_marker_is_not_truncated(self) -> None:
        """Why the count is used instead of searching for `<?xml`: a message that quotes
        one would otherwise cut itself in half."""
        body = 'Here is the config: <?xml version="1.0"?> — send it back by Friday.'
        message = parse_emlx(emlx_bytes(body=body))
        assert message is not None
        assert "send it back by Friday" in body_text(message)

    def test_a_file_that_is_not_an_emlx_is_skipped_not_guessed_at(self) -> None:
        assert parse_emlx(b"not an emlx at all") is None
        assert parse_emlx(b"") is None

    def test_quoted_printable_is_decoded(self) -> None:
        """A body read raw is full of `=20` and a commitment inside it does not survive
        extraction. `get_body` applies the transfer encoding; hand-rolled parsing does not."""
        message = parse_emlx(emlx_bytes(body="Send the form — by Friday, please."))
        assert message is not None
        assert "by Friday" in body_text(message)
        assert "=20" not in body_text(message)


class TestFetch:
    def test_a_message_becomes_a_source_item(self, tmp_path: Path, boundary: Boundary) -> None:
        root = build_store(tmp_path, [{"rowid": 1, "date_received": NOW}])
        items = list(connector(root, boundary).fetch(None))

        assert len(items) == 1
        item = items[0]
        assert item.source == "apple-mail"
        assert item.external_id == "<abc-123@asu.edu>"
        assert item.title == "MLSBE paperwork"
        assert "signed form" in (item.body_text or "")

    def test_the_senders_offset_is_preserved(self, tmp_path: Path, boundary: Boundary) -> None:
        """CLAUDE.md rule 4. "By Friday" written at 09:30 in Phoenix is a different Friday
        from one written at 09:30 in Coimbatore, and only the offset says which."""
        root = build_store(tmp_path, [{"rowid": 1, "date_received": NOW}])
        assert list(connector(root, boundary).fetch(None))[0].occurred_at == SENT_ISO

    def test_one_email_in_two_folders_is_one_item(
        self, tmp_path: Path, boundary: Boundary
    ) -> None:
        """A Gmail account exposes INBOX and [Gmail]/All Mail as separate folders holding
        the same message. Two index rows, two files, one commitment."""
        root = build_store(
            tmp_path,
            [
                {"rowid": 1, "date_received": NOW, "mailbox": "imap://A/INBOX"},
                {
                    "rowid": 2,
                    "date_received": NOW,
                    "mailbox": "imap://A/%5BGmail%5D/All%20Mail",
                },
            ],
        )
        items = list(connector(root, boundary).fetch(None))
        assert len(items) == 1

    def test_junk_and_drafts_are_not_ingested(
        self, tmp_path: Path, boundary: Boundary
    ) -> None:
        root = build_store(
            tmp_path,
            [
                {"rowid": 1, "date_received": NOW, "mailbox": "imap://A/Junk"},
                {"rowid": 2, "date_received": NOW, "mailbox": "imap://A/Drafts"},
                {
                    "rowid": 3,
                    "date_received": NOW,
                    "mailbox": "imap://A/Deleted%20Messages",
                },
            ],
        )
        assert list(connector(root, boundary).fetch(None)) == []

    def test_the_window_bounds_the_first_run(self, tmp_path: Path, boundary: Boundary) -> None:
        old = int((datetime.now(UTC) - timedelta(days=400)).timestamp())
        root = build_store(
            tmp_path,
            [
                {"rowid": 1, "date_received": old},
                {"rowid": 2, "date_received": NOW, "message": {"message_id": "<new@x>"}},
            ],
        )
        items = list(connector(root, boundary).fetch(None))
        assert [i.external_id for i in items] == ["<new@x>"]

    def test_the_cursor_resumes(self, tmp_path: Path, boundary: Boundary) -> None:
        earlier = NOW - 3600
        root = build_store(
            tmp_path,
            [
                {"rowid": 1, "date_received": earlier},
                {"rowid": 2, "date_received": NOW, "message": {"message_id": "<later@x>"}},
            ],
        )
        first = connector(root, boundary)
        list(first.fetch(None))

        second = connector(root, boundary)
        again = list(second.fetch(first.cursor))
        # Inclusive of the boundary second, so the last message is re-read and dedup
        # makes that free. What must not appear is the earlier one.
        assert "<abc-123@asu.edu>" not in [i.external_id for i in again]

    def test_a_missing_file_does_not_advance_the_watermark(
        self, tmp_path: Path, boundary: Boundary
    ) -> None:
        """A body still downloading is a temporary state, not a decision. Advancing past
        it would lose the message permanently once it arrives."""
        root = build_store(
            tmp_path,
            [
                {"rowid": 1, "date_received": NOW - 60, "message": {"message_id": "<got@x>"}},
                {"rowid": 2, "date_received": NOW, "no_file": True},
            ],
        )
        conn = connector(root, boundary)
        list(conn.fetch(None))

        # The undownloaded message is the *newest* one, so a watermark that advanced past
        # it would never come back for it.
        assert conn.cursor is not None
        assert datetime.fromisoformat(conn.cursor).timestamp() == NOW - 60

        # And once the body lands, the next run picks it up.
        folder = root / "V10" / "ACCOUNT-A" / "box.mbox" / "Data" / "Messages"
        (folder / "2.emlx").write_bytes(emlx_bytes(message_id="<arrived@x>"))
        later = connector(root, boundary)
        assert "<arrived@x>" in [i.external_id for i in later.fetch(conn.cursor)]

    def test_a_partially_downloaded_body_is_still_read(
        self, tmp_path: Path, boundary: Boundary
    ) -> None:
        root = build_store(
            tmp_path, [{"rowid": 1, "date_received": NOW, "partial": True}]
        )
        assert len(list(connector(root, boundary).fetch(None))) == 1


class TestIdempotency:
    def test_a_second_run_produces_the_same_hashes(
        self, tmp_path: Path, boundary: Boundary
    ) -> None:
        """Rule 3, at the connector's level: the same store yields byte-identical items,
        so the ledger's hash check writes nothing on the second pass."""
        root = build_store(
            tmp_path,
            [
                {"rowid": 1, "date_received": NOW},
                {"rowid": 2, "date_received": NOW, "message": {"message_id": "<two@x>"}},
            ],
        )
        first = [i.content_hash for i in connector(root, boundary).fetch(None)]
        second = [i.content_hash for i in connector(root, boundary).fetch(None)]
        assert first == second and len(first) == 2


class TestHealth:
    def test_a_readable_store_is_healthy(self, tmp_path: Path, boundary: Boundary) -> None:
        root = build_store(tmp_path, [{"rowid": 1, "date_received": NOW}])
        assert connector(root, boundary).health().ok

    def test_a_missing_store_says_full_disk_access(
        self, tmp_path: Path, boundary: Boundary
    ) -> None:
        """macOS hides ~/Library/Mail from unapproved processes as if it were not there,
        so "not found" must carry the real instruction rather than sending the owner to
        look for a directory that is sitting right where they left it."""
        health = connector(tmp_path / "nothing", boundary).health()
        assert not health.ok
        assert "Full Disk Access" in (health.detail or "")

    def test_the_newest_version_directory_wins(
        self, tmp_path: Path, boundary: Boundary
    ) -> None:
        """V10 sorts above V9, which a string comparison gets backwards — and a macOS
        upgrade must not look like a source that stopped collecting."""
        build_store(tmp_path, [{"rowid": 1, "date_received": NOW}])
        (tmp_path / "V9" / "MailData").mkdir(parents=True)
        assert connector(tmp_path, boundary).store_root().name == "V10"


class TestTheBoundary:
    def test_a_denylisted_sender_produces_no_row(
        self, tmp_path: Path, enforcing: Boundary
    ) -> None:
        root = build_store(
            tmp_path,
            [
                {
                    "rowid": 1,
                    "date_received": NOW,
                    "message": {"sender": "Dana <dana@clientexample.gov>"},
                }
            ],
        )
        conn = connector(root, enforcing)
        assert list(conn.fetch(None)) == []
        assert conn.excluded == 1
        assert conn.excluded_by_rule == {"clientexample.gov": 1}

    def test_a_denylisted_address_in_cc_produces_no_row(
        self, tmp_path: Path, enforcing: Boundary
    ) -> None:
        """docs/08 D7: From, To, Cc or Bcc — any of them, and the whole message goes."""
        root = build_store(
            tmp_path,
            [
                {
                    "rowid": 1,
                    "date_received": NOW,
                    "message": {"cc": "wic@wic.clientexample.gov"},
                }
            ],
        )
        assert list(connector(root, enforcing).fetch(None)) == []

    def test_the_excluded_address_is_never_retained(
        self, tmp_path: Path, enforcing: Boundary
    ) -> None:
        """Only the rule that fired is recorded. The address is excluded content, and
        docs/08 forbids retaining it "in any form, including hashes"."""
        root = build_store(
            tmp_path,
            [
                {
                    "rowid": 1,
                    "date_received": NOW,
                    "message": {"sender": "dana@clientexample.gov"},
                }
            ],
        )
        conn = connector(root, enforcing)
        list(conn.fetch(None))
        assert "dana" not in str(conn.excluded_by_rule)


def test_mailbox_name_decodes_the_url() -> None:
    assert mailbox_name("imap://UUID/%5BGmail%5D/All%20Mail") == "All Mail"
    assert mailbox_name("imap://UUID/INBOX") == "INBOX"


class TestTheAccountsInScope:
    """docs/08 §The decision as made. The denylist is empty because the out-of-scope
    mailbox is not connected — so what has to be enforced is the account list itself."""

    def test_mail_delivered_to_an_out_of_scope_account_is_not_stored(
        self, tmp_path: Path, boundary: Boundary
    ) -> None:
        root = build_store(
            tmp_path,
            [
                {
                    "rowid": 1,
                    "date_received": NOW,
                    "raw": emlx_with_delivered_to("parent@example.com"),
                },
                {
                    "rowid": 2,
                    "date_received": NOW,
                    "raw": emlx_with_delivered_to("dkesava2@asu.edu", "<mine@x>"),
                },
            ],
        )
        conn = connector(
            root, boundary, out_of_scope_accounts=frozenset({"parent@example.com"})
        )
        items = list(conn.fetch(None))

        assert [i.external_id for i in items] == ["<mine@x>"]
        assert conn.excluded_by_rule == {"out-of-scope-account": 1}

    def test_the_account_is_recorded_so_a_new_mailbox_is_visible(
        self, tmp_path: Path, boundary: Boundary
    ) -> None:
        """The check that survives the owner forgetting: doctor prints what was read."""
        import json

        root = build_store(
            tmp_path,
            [{"rowid": 1, "date_received": NOW, "raw": emlx_with_delivered_to("me@asu.edu")}],
        )
        item = list(connector(root, boundary).fetch(None))[0]
        assert json.loads(item.raw_json)["delivered_to"] == "me@asu.edu"

    def test_the_comparison_is_case_insensitive(
        self, tmp_path: Path, boundary: Boundary
    ) -> None:
        root = build_store(
            tmp_path,
            [{"rowid": 1, "date_received": NOW,
              "raw": emlx_with_delivered_to("Parent@Example.COM")}],
        )
        conn = connector(
            root, boundary, out_of_scope_accounts=frozenset({"parent@example.com"})
        )
        assert list(conn.fetch(None)) == []
