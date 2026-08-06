"""Mail, from the copy macOS already keeps on disk.

docs/07 §Gmail describes reaching mail through the Gmail API, and on this machine that
path was never usable: it needs an OAuth client the owner would have to register, a
consent screen, and a token to refresh, and none of it survives contact with a personal
laptop that already has the mail sitting in it. Mail.app holds the same messages,
already synced, already authenticated, under a permission the project holds — the same
realisation that made `apple_calendar.py` possible after months of the Google connector
being the documented answer. The API for a service and the data from it are different
questions.

What the store looks like
------------------------
`~/Library/Mail/V10` holds one directory per account plus `MailData/Envelope Index`, a
SQLite database. Content and index are split, and this connector uses both for what each
is good at:

  * **`Envelope Index` selects.** `messages` carries one row per message per mailbox with
    `date_received`, `date_sent` and a `mailbox` foreign key; `mailboxes.url` names the
    account UUID and folder. Cheap to scan, and it is how a run decides *which* messages
    it has not read yet without opening 35,000 files.
  * **`.emlx` files carry the message.** Each is a byte count, a newline, exactly that
    many bytes of RFC822, and an Apple plist of flags. The RFC822 part is parsed with
    the stdlib `email` package, so headers, MIME structure and transfer encodings are
    somebody else's problem. Everything the ledger stores — sender, subject, body, the
    `Date` offset — comes from the file, never from the index, so there is one source of
    truth per message.

Cursoring on time, not on ROWID
-------------------------------
The obvious watermark is `messages.ROWID`, and it is wrong. Mail rebuilds the Envelope
Index — after a crash, an upgrade, or a "reindex mailbox" — and a rebuilt index
renumbers every row, so a ROWID watermark would either re-read the entire store or, far
worse, sit above rows that are now numbered below it and skip mail permanently. The
cursor is therefore `date_received`, which is a property of the message rather than of
the index, and the window is inclusive of it: the boundary second is re-read on every
run and `content_hash` makes that cost nothing (rule 3).

The same message twice
----------------------
A Gmail account exposes `INBOX` and `[Gmail]/All Mail` as separate IMAP folders holding
the same message, so the index has two rows and the disk has two `.emlx` files for one
email. They are one message and they carry one commitment, so `external_id` is the
RFC822 `Message-ID` rather than anything positional — which also means a reindex, a
re-download, or the owner moving a message between folders does not create a second
row in the ledger.

Deliberately not ingested
-------------------------
Junk, Spam, Trash, Deleted Messages and Drafts. A draft is something the owner has not
said yet, and the other four are, by the owner's own filing, not things they owe anyone.
Attachments are not read; docs/08's rule about inherited ownership applies to them and
the body is where commitments are written.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote

from backglass.connectors.base import Cursor, Health, SourceItem, content_hash
from backglass.connectors.boundary import Boundary, addresses_in
from backglass.connectors.gmail import strip_quoted

#: Folders whose contents are not commitments. Matched on the last path segment of
#: `mailboxes.url`, case-folded, after percent-decoding — Mail stores "Deleted
#: Messages" as `Deleted%20Messages`.
SKIPPED_MAILBOXES = frozenset(
    {"junk", "spam", "trash", "deleted messages", "drafts", "outbox", "sendlater"}
)

#: Selection only: which messages this run has not seen. Everything the ledger keeps is
#: read from the .emlx file, so this query deliberately fetches no content.
_QUERY = """
SELECT messages.ROWID       AS rowid,
       messages.date_received AS date_received,
       mailboxes.url        AS mailbox_url
FROM messages
JOIN mailboxes ON mailboxes.ROWID = messages.mailbox
WHERE messages.date_received >= ?
  AND COALESCE(messages.deleted, 0) = 0
ORDER BY messages.date_received
"""


def parse_emlx(raw: bytes) -> EmailMessage | None:
    """The RFC822 message inside an .emlx wrapper.

    The file is `<byte count>\\n<message bytes>\\n<plist>`. Slicing by the declared count
    rather than searching for the plist is what keeps a message containing the literal
    text `<?xml` from truncating itself. A count that does not parse means the file is
    something other than an .emlx, and is skipped rather than guessed at.
    """
    head, newline, rest = raw.partition(b"\n")
    if not newline:
        return None
    try:
        length = int(head.strip())
    except ValueError:
        return None
    message = message_from_bytes(rest[:length], policy=policy.default)
    return message if isinstance(message, EmailMessage) else None


def body_text(message: EmailMessage) -> str:
    """Prefer text/plain, fall back to text/html with tags stripped.

    `get_body` walks the MIME tree and applies the transfer encoding and charset, which
    is the part hand-rolled mail parsing gets wrong — a quoted-printable body read raw is
    full of `=20` and a commitment written in it does not survive extraction.
    """
    try:
        part = message.get_body(preferencelist=("plain", "html"))
    except Exception:  # noqa: BLE001 - a malformed tree is a skipped body, not a crash
        return ""
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, ValueError):
        # An unknown charset or a broken encoding. Bytes are still better than nothing —
        # but `get_payload(decode=True)` returns bytes only for a leaf part; on a
        # multipart it hands back the list of sub-messages, which has no `.decode`.
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            return ""
        content = payload.decode("utf-8", errors="replace")
    if not isinstance(content, str):
        return ""
    if part.get_content_type() == "text/html":
        from backglass.connectors.gmail import _TAG

        return _TAG.sub(" ", content)
    return content


def occurred_at_of(message: EmailMessage, date_received: int) -> str:
    """When it was sent, with the sender's UTC offset intact.

    CLAUDE.md rule 4 resolves every relative date against this, and the offset is what
    makes "by Friday" mean the same Friday to a reader in Phoenix and to one in
    Coimbatore. `date_received` is a bare epoch — it has already thrown the offset away —
    so it is only the fallback for a message whose `Date` header is missing or unparseable.
    """
    raw = message.get("Date")
    if raw:
        try:
            parsed = parsedate_to_datetime(str(raw))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.replace(microsecond=0).isoformat()
        except (TypeError, ValueError):
            pass
    return datetime.fromtimestamp(date_received, tz=UTC).replace(microsecond=0).isoformat()


def mailbox_name(url: str) -> str:
    """The folder, as a person would name it:
    `imap://UUID/%5BGmail%5D/All%20Mail` → `All Mail`."""
    return unquote(url).rstrip("/").rpartition("/")[2]


def account_of(url: str) -> str:
    """The account UUID a mailbox belongs to. Kept in raw_json so evidence can say which
    inbox a claim came from without the ledger growing a column for it."""
    trimmed = unquote(url).split("//", 1)[-1]
    return trimmed.split("/", 1)[0]


@dataclass(kw_only=True)
class AppleMailConnector:
    """Mail.app's local store. One message in, one SourceItem out."""

    mail_root: Path
    boundary: Boundary
    #: How far back a first run reaches. The archive on this machine goes to 2019 and a
    #: commitment ledger has no use for a 2021 newsletter; the cursor keeps every later
    #: run cheap regardless.
    lookback_days: int = 120
    #: Mailboxes that must never be read, by the address mail is delivered to. docs/08's
    #: decision of 2026-08-03 rests on *which accounts are connected* rather than on a
    #: denylist of correspondents, and Mail.app will happily gain an account without
    #: anything in this repo changing. Checked here, before persistence, because that is
    #: what D1 requires and because a filter over stored rows would be a filter applied
    #: after the data was already in.
    out_of_scope_accounts: frozenset[str] = frozenset()

    cursor: Cursor = None
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)

    #: Built once per fetch: ROWID → path of the .emlx holding that message. Walking the
    #: tree beats deriving the path from the ROWID (`Data/<d2>/<d1>/Messages/<id>.emlx`,
    #: with the digits reversed and dropped entirely below 1000) because that layout is
    #: Apple's private business, and a `.partial.emlx` — a message whose body has not
    #: finished downloading — does not follow it at all.
    _files: dict[str, Path] = field(default_factory=dict, repr=False)

    @property
    def name(self) -> str:
        return "apple-mail"

    def store_root(self) -> Path:
        """The versioned directory holding `MailData`, resolved rather than pinned.

        `mail_root` is `~/Library/Mail`, whose child is `V10` today and `V11` after the
        next macOS upgrade. Pinning the version in `.env` would make an OS update look
        exactly like a source that stopped collecting — silently, since a missing
        directory and an empty mailbox are the same absence downstream. Highest version
        wins; a path that already contains `MailData` (an archived store) is used as-is.
        """
        if (self.mail_root / "MailData").is_dir():
            return self.mail_root
        versions = sorted(
            (p for p in self.mail_root.glob("V*") if (p / "MailData").is_dir()),
            key=lambda p: _version_key(p.name),
            reverse=True,
        )
        return versions[0] if versions else self.mail_root

    def _index_path(self) -> Path:
        return self.store_root() / "MailData" / "Envelope Index"

    def health(self) -> Health:
        index = self._index_path()
        if not index.exists():
            return Health(
                name=self.name,
                ok=False,
                detail=(
                    f"no Mail index at {index} — if Mail.app is set up on this Mac, grant "
                    "Full Disk Access to the program running Backglass (System Settings → "
                    "Privacy & Security → Full Disk Access); macOS hides ~/Library/Mail "
                    "from unapproved processes as if it were not there"
                ),
            )
        try:
            with closing(self._connect()) as conn:
                conn.execute("SELECT ROWID FROM messages LIMIT 1").fetchone()
        except sqlite3.Error as exc:
            return Health(
                name=self.name,
                ok=False,
                detail=(
                    f"cannot read {index}: {type(exc).__name__}: {exc} — the usual cause "
                    "is missing Full Disk Access for the program running Backglass. "
                    "Scheduled runs go through uv, so add the uv binary as well as the "
                    "terminal"
                ),
            )
        return Health(name=self.name, ok=True)

    def _connect(self) -> sqlite3.Connection:
        # mode=ro and NOT immutable=1: the Envelope Index is WAL and Mail.app is usually
        # open, so immutable would skip the -wal file and read a store that is silently
        # hours stale. Same rule as anki.py and imessage.py.
        conn = sqlite3.connect(f"file:{self._index_path()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _scan_files(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        for path in self.store_root().rglob("*.emlx"):
            # "38368.partial.emlx" is a body still downloading; its headers are complete
            # and a partial body is better evidence than none.
            found.setdefault(path.name.split(".", 1)[0], path)
        return found

    def fetch(self, since: Cursor) -> Iterator[SourceItem]:
        """Yield messages received at or after the cursor, inside the window.

        The watermark only advances past messages that were actually read. A message
        skipped because its file has not been downloaded yet must not move the cursor
        past itself, or it is never seen again — unlike the iMessage connector, where a
        rejected row is a settled decision, a missing .emlx is a temporary state.
        """
        self.excluded = 0
        self.excluded_by_rule = {}
        self._files = {}

        floor = datetime.now(UTC) - timedelta(days=self.lookback_days)
        watermark = max(_epoch(since), int(floor.timestamp()))
        highest = watermark
        seen_messages: set[str] = set()

        with closing(self._connect()) as conn:
            rows = conn.execute(_QUERY, (watermark,)).fetchall()

        if rows:
            self._files = self._scan_files()

        for row in rows:
            mailbox = mailbox_name(str(row["mailbox_url"]))
            if mailbox.casefold() in SKIPPED_MAILBOXES:
                # Not an exclusion in the docs/08 sense — nothing was denied, the folder
                # is simply not a place commitments live. Advancing past it is correct.
                highest = max(highest, int(row["date_received"] or 0))
                continue
            item = self._to_item(row, mailbox, seen_messages)
            if item is _SKIP_NO_FILE:
                continue
            highest = max(highest, int(row["date_received"] or 0))
            if isinstance(item, SourceItem):
                yield item

        self.cursor = datetime.fromtimestamp(highest, tz=UTC).isoformat()

    def _to_item(
        self, row: sqlite3.Row, mailbox: str, seen_messages: set[str]
    ) -> SourceItem | None | object:
        path = self._files.get(str(row["rowid"]))
        if path is None:
            return _SKIP_NO_FILE
        try:
            message = parse_emlx(path.read_bytes())
        except OSError:
            return _SKIP_NO_FILE
        if message is None:
            return None

        headers = {key: str(value) for key, value in message.items()}

        account = delivered_to(headers)
        if account and account in self.out_of_scope_accounts:
            self.excluded += 1
            rule = "out-of-scope-account"
            self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + 1
            return None

        verdict = self.boundary.check(addresses_in(headers))
        if not verdict.allowed:
            # docs/08 D4: no row, no hash, no model call. Only the rule that fired is
            # recorded — the address is excluded content and does not get retained.
            self.excluded += 1
            rule = verdict.matched_rule or "boundary"
            self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + 1
            return None

        # One email, one ledger row, however many folders hold a copy of it.
        message_id = str(message.get("Message-ID") or "").strip()
        external_id = message_id or f"rowid:{row['rowid']}"
        if external_id in seen_messages:
            return None
        seen_messages.add(external_id)

        occurred_at = occurred_at_of(message, int(row["date_received"] or 0))
        author = str(message.get("From") or "")
        title = str(message.get("Subject") or "")
        body = strip_quoted(body_text(message))
        return SourceItem(
            source=self.name,
            external_id=external_id,
            occurred_at=occurred_at,
            content_hash=content_hash(
                author=author, title=title, body_text=body, occurred_at=occurred_at
            ),
            author=author,
            title=title,
            body_text=body,
            raw_json=_raw_json(mailbox, str(row["mailbox_url"]), headers),
        )


#: Returned instead of None when the message could not be read *this time*. None means
#: "read and deliberately not stored" and lets the watermark advance; this does not.
_SKIP_NO_FILE = object()


def delivered_to(headers: dict[str, str]) -> str:
    """Which of the owner's own mailboxes received this — the account, as an address.

    Mail's store names accounts by UUID and keeps the address they belong to somewhere
    TCC-protected and undocumented, so the reliable answer is in the message: an IMAP
    delivery carries `Delivered-To`, and `X-Original-To` covers the servers that do not.
    This is what makes docs/08's decision auditable rather than a promise — the boundary
    there rests on *which accounts are connected*, and this is the only way the system can
    notice that a new one appeared.

    Not excluded content: it is the owner's own address, on a message the boundary already
    allowed. A denied message never reaches here.
    """
    for header in ("Delivered-To", "X-Original-To", "X-Envelope-To"):
        value = headers.get(header, "").strip().lower()
        if value:
            return value.strip("<>")
    return ""


def _raw_json(mailbox: str, url: str, headers: dict[str, str]) -> str:
    import json

    return json.dumps(
        {
            "mailbox": mailbox,
            "account": account_of(url),
            "delivered_to": delivered_to(headers),
            # Enough to reconstruct a thread without storing the recipients themselves.
            "in_reply_to": headers.get("In-Reply-To", ""),
        }
    )


def _version_key(name: str) -> tuple[int, str]:
    """`V10` sorts above `V9`, which a plain string comparison gets backwards."""
    digits = name[1:]
    return (int(digits), name) if digits.isdigit() else (-1, name)


def _epoch(cursor: Cursor) -> int:
    if not cursor:
        return 0
    try:
        return int(datetime.fromisoformat(str(cursor)).timestamp())
    except ValueError:
        return 0
