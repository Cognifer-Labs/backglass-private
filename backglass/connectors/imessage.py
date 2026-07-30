"""iMessage. docs/07 §Connectors, in the shape of §Notes rather than §Gmail.

Like Obsidian, this is a local source: no network call, no OAuth, no credential. The
"auth expired" state does not exist; the state that does exist is macOS refusing to open
the file, which is what `health()` is for.

What the Messages store looks like
----------------------------------
The store is a SQLite database, `~/Library/Messages/chat.db` in production. The path is
injected rather than hardcoded so tests can build a fixture db, and so the owner can
point at a copied or archived store.

  * `message.date` is an offset from the **Apple epoch**, 2001-01-01 00:00:00 UTC — not
    the Unix epoch. Since macOS 10.13 it is in **nanoseconds**; older stores and most
    third-party exports hold **seconds**. Both are handled, by magnitude: a seconds value
    for any plausible message is below 1e11 (that is the year 5138), and a nanosecond
    value for anything after 2001 is far above it.
  * `message.handle_id` → `handle.ROWID`. `handle.id` is the counterparty's phone number
    or Apple ID email — an address, which is why the docs/08 boundary genuinely applies
    here and not just as ceremony.
  * `chat_message_join` maps a message to a `chat`, which carries a display name for
    group threads. One-to-one chats usually have none, so the handle stands in.
  * `message.is_from_me` is the direction flag: 1 for the owner, 0 for the counterparty.
  * `message.text` is NULL on newer rows whose content lives in `attributedBody`, an
    NSAttributedString typedstream blob. Decoding it means either a typedstream parser or
    a pyobjc dependency, and neither is worth it for a source that is already partial —
    those rows are skipped rather than half-decoded. If the owner ever finds the gap
    material, that is the place to look.

The connection is opened `mode=ro&immutable=1`. Read-only is the obvious half; immutable
is the load-bearing half, because it stops SQLite taking any lock at all on a database
Messages.app has open and is actively writing to.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backglass.connectors.base import Cursor, Health, SourceItem, content_hash
from backglass.connectors.boundary import Boundary

#: 2001-01-01 00:00:00 UTC. Apple's epoch, 31 years after everyone else's.
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)

#: Above this, `message.date` is nanoseconds; below it, seconds. See the module docstring.
NANOSECOND_THRESHOLD = 100_000_000_000

_QUERY = """
SELECT
    message.ROWID        AS rowid,
    message.date         AS date,
    message.text         AS text,
    message.is_from_me   AS is_from_me,
    handle.id            AS handle,
    chat.display_name    AS chat_name
FROM message
LEFT JOIN handle ON handle.ROWID = message.handle_id
LEFT JOIN chat_message_join ON chat_message_join.message_id = message.ROWID
LEFT JOIN chat ON chat.ROWID = chat_message_join.chat_id
WHERE message.ROWID > ?
ORDER BY message.ROWID
"""


@dataclass(kw_only=True)
class IMessageConnector:
    """The local Messages store. One message in, one SourceItem out.

    Nothing is grouped into threads. A thread is a *derived* view and the ledger does not
    need one: each message is its own evidence, and a commitment extracted from a single
    text links to the single text it came from (CLAUDE.md rule 1).
    """

    db_path: Path
    boundary: Boundary

    #: Only final once `fetch()` has been exhausted.
    cursor: Cursor = None
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return "imessage"

    def health(self) -> Health:
        if not self.db_path.exists():
            return Health(
                name=self.name,
                ok=False,
                detail=(
                    f"Messages store not found at {self.db_path} — if the path is right, "
                    "grant Full Disk Access to the terminal running Backglass "
                    "(System Settings → Privacy & Security → Full Disk Access); macOS "
                    "hides chat.db from unapproved processes as if it did not exist"
                ),
            )
        try:
            with closing(self._connect()) as conn:
                conn.execute("SELECT ROWID FROM message LIMIT 1").fetchone()
        except sqlite3.Error as exc:
            return Health(
                name=self.name,
                ok=False,
                detail=(
                    f"cannot read {self.db_path}: {type(exc).__name__}: {exc} — the usual "
                    "cause is missing Full Disk Access for the terminal running Backglass "
                    "(System Settings → Privacy & Security → Full Disk Access)"
                ),
            )
        return Health(name=self.name, ok=True)

    def fetch(self, since: Cursor) -> Iterator[SourceItem]:
        """Yield messages with a ROWID above the cursor.

        ROWID is monotonic in the Messages store — rows are appended, never renumbered —
        so the highest one seen is a complete watermark. `since=None` is a full scan,
        which is the first run and the only full scan there ever is.
        """
        self.excluded = 0
        self.excluded_by_rule = {}

        watermark = _parse(since)
        highest = watermark

        with closing(self._connect()) as conn:
            for row in conn.execute(_QUERY, (watermark,)):
                rowid = int(row["rowid"])
                # Advance past skipped rows too. A NULL-text row that never moved the
                # watermark would be re-read on every run forever.
                highest = max(highest, rowid)
                item = self._to_item(row)
                if item is not None:
                    yield item

        self.cursor = str(highest)

    def _connect(self) -> sqlite3.Connection:
        # immutable=1 promises SQLite the file will not change under it, which is what
        # buys a lock-free read of a database Messages.app has open. It is a promise about
        # *our* behaviour, not the store's: we never write, so we never break it.
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _to_item(self, row: sqlite3.Row) -> SourceItem | None:
        handle = (row["handle"] or "").strip()
        if not handle:
            # No handle means a system row — a group rename, someone leaving a thread.
            # There is no counterparty and no content worth a model call.
            return None

        text = (row["text"] or "").strip()
        if not text:
            # Either genuinely empty, or an attributedBody-only row. See the docstring.
            return None

        # D1/D4. Before persistence, before hashing, before any model call. The handle is
        # an address in exactly the sense docs/08 means, so it is checked like one.
        verdict = self.boundary.check([handle])
        if not verdict.allowed:
            self.excluded += 1
            rule = verdict.matched_rule or "?"
            self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + 1
            return None

        is_from_me = bool(row["is_from_me"])
        chat_name = (row["chat_name"] or "").strip() or None
        occurred_at = apple_time_to_iso(row["date"])
        author = "me" if is_from_me else handle
        title = chat_name or handle

        return SourceItem(
            source=self.name,
            external_id=str(int(row["rowid"])),
            occurred_at=occurred_at,
            author=author,
            title=title,
            body_text=text,
            raw_json=json.dumps(
                {"chat": chat_name, "is_from_me": is_from_me, "handle": handle},
                sort_keys=True,
            ),
            content_hash=content_hash(
                author=author, title=title, body_text=text, occurred_at=occurred_at
            ),
        )


def apple_time_to_iso(value: int | float | None) -> str:
    """Convert a `message.date` to an ISO-8601 UTC timestamp.

    Handles both the nanosecond form (macOS 10.13+) and the second form (older stores and
    exports) by magnitude, per the module docstring. CLAUDE.md rule 4 leans on this: a
    text saying "by Friday" resolves against *this* timestamp, so an off-by-31-years
    conversion would be the most damaging bug in the connector.
    """
    raw = int(value or 0)
    seconds = raw / 1_000_000_000 if abs(raw) >= NANOSECOND_THRESHOLD else float(raw)
    moment = APPLE_EPOCH + timedelta(seconds=seconds)
    return moment.replace(microsecond=0).isoformat()


def _parse(cursor: Cursor) -> int:
    """The cursor is a stringified ROWID. Anything unreadable means a full scan."""
    if not cursor:
        return 0
    try:
        return int(str(cursor))
    except ValueError:
        return 0
