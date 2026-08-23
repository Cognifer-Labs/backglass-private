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
  * `message.text` is NULL on a large share of post-Ventura rows, whose content lives in
    `attributedBody` — an NSAttributedString typedstream blob. Those rows are recovered by
    `_typedstream.extract_text`, a vendored NSString-marker scan rather than a dependency.
    A blob that will not decode leaves the row textless, and it is skipped as before.
  * `message.associated_message_type` is non-zero on tapbacks: 2000–2005 is a reaction
    added, 3000–3005 the same reaction removed. They are real rows carrying real text
    (`Loved “the deck is done”`), so unfiltered, a heart lands in the ledger as a message.
  * Edited messages get no special handling, deliberately. An edit rewrites the content of
    a ROWID that is already stored, so the ledger's upsert records it as an immutability
    conflict — which is the right outcome, because what the owner was told at the time is
    the evidence, and the edit is a later event rather than a correction of the record.

The connection is opened plain ``mode=ro`` with a short busy timeout — deliberately
*not* ``immutable=1``, because chat.db is in WAL mode and immutable makes SQLite skip
the ``-wal`` file entirely: against a database Messages.app has open, reads are either
silently stale or fail with "no such table". Same failure mode the Anki and Avorio
connectors hit; see anki.py's docstring.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from backglass.chats import Sighting
from backglass.connectors import _typedstream
from backglass.connectors.allowlist import Allowlist
from backglass.connectors.base import Cursor, Health, SourceItem, content_hash
from backglass.connectors.boundary import Boundary

#: 2001-01-01 00:00:00 UTC. Apple's epoch, 31 years after everyone else's.
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)

#: Above this, `message.date` is nanoseconds; below it, seconds. See the module docstring.
NANOSECOND_THRESHOLD = 100_000_000_000

#: `associated_message_type` for a tapback: 2000–2005 added, 3000–3005 removed. Zero is an
#: ordinary message; the range between the two blocks is unused, so one span covers both.
TAPBACK_RANGE = range(2000, 3006)

_QUERY = """
SELECT
    message.ROWID                          AS rowid,
    message.date                           AS date,
    message.text                           AS text,
    message.attributedBody                 AS attributed_body,
    message.associated_message_type        AS associated_message_type,
    message.is_from_me                     AS is_from_me,
    handle.id                              AS handle,
    chat.display_name                      AS chat_name
FROM message
LEFT JOIN handle ON handle.ROWID = message.handle_id
LEFT JOIN chat_message_join ON chat_message_join.message_id = message.ROWID
LEFT JOIN chat ON chat.ROWID = chat_message_join.chat_id
WHERE message.ROWID > ?
  -- Bounding the window in SQL is the point: without it the first run walks the whole
  -- archive before Python ever sees a row.
  --
  -- The column is normalised to SECONDS before comparing, by the same magnitude test the
  -- Python side uses. Modern stores hold nanoseconds since 2001-01-01 and older ones (and
  -- third-party exports) hold seconds; comparing a raw column against a nanosecond
  -- threshold silently drops every legacy-format row, which is a filter that looks like
  -- an empty archive.
  AND (CASE WHEN ABS(message.date) >= ? THEN message.date / 1000000000 ELSE message.date END)
      >= (strftime('%s', 'now', ?) - strftime('%s', '2001-01-01'))
ORDER BY message.ROWID
"""

#: What conversations exist, and how loud each one is, over the same window `_QUERY`
#: bounds — and deliberately *not* over the same cursor. Grouped in SQL because the
#: answer is a list of names, not a list of messages: 43,000 rows collapse to fifty.
#: `NULLIF` on the display name is what makes a one-to-one fall back to the handle,
#: matching `_allowed`'s two cases exactly.
_DISCOVER_QUERY = """
SELECT COALESCE(NULLIF(chat.display_name, ''), handle.id)              AS name,
       chat.display_name IS NOT NULL AND chat.display_name != ''       AS is_group,
       COUNT(*)                                                        AS n
FROM message
LEFT JOIN handle ON handle.ROWID = message.handle_id
LEFT JOIN chat_message_join ON chat_message_join.message_id = message.ROWID
LEFT JOIN chat ON chat.ROWID = chat_message_join.chat_id
WHERE (CASE WHEN ABS(message.date) >= 100000000000
            THEN message.date / 1000000000 ELSE message.date END)
      >= (strftime('%s', 'now', ?) - strftime('%s', '2001-01-01'))
GROUP BY name
ORDER BY n DESC
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
    #: The only conversations read. An inbox-wide read of a personal message store is the
    #: wrong default — most of what is in there is other people's words about things the
    #: owner never meant to file. Same rule and same class as Instagram's; see
    #: connectors/allowlist.py. An empty allowlist makes the connector unhealthy rather
    #: than making it read everything.
    allowlist: Allowlist = field(default_factory=lambda: Allowlist(()))
    #: How far back a scan reaches, in days. The cursor already stops the connector
    #: re-reading what it has seen; this stops the FIRST run reaching back over an entire
    #: archive — 42,879 messages on the owner's machine, where the useful window for a
    #: commitment ledger is the last few months.
    lookback_days: int = 90

    #: Only final once `fetch()` has been exhausted.
    cursor: Cursor = None
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)
    #: Every conversation in the lookback window, allowed or not, keyed the way the
    #: allowlist matches. Reported rather than written: a connector emits SourceItems and
    #: nothing else, so `sync` is what records these — the same seam `excluded_by_rule`
    #: uses. This is what lets a chat nobody has named surface as a question instead of
    #: being dropped in silence.
    #:
    #: Filled by `discover()` over the whole window rather than by the fetch loop, which
    #: only ever sees rows above the cursor. Gathering them in the loop meant a chat was
    #: offered for a decision only if it had spoken since the last sync — so the quiet
    #: conversations, and every conversation at all on a machine whose cursor was already
    #: current, could never appear on the page whose entire purpose is to list them.
    seen_chats: dict[str, Sighting] = field(default_factory=dict)
    #: The counts above are window totals, recomputed each run, not increments. `sync`
    #: reads this to know it must overwrite rather than add.
    sightings_are_cumulative: bool = False

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
        if not self.allowlist:
            return Health(
                name=self.name,
                ok=False,
                detail=(
                    "no conversations are monitored yet — open /chats to choose, or run "
                    "`backglass imessage chats`. The sync still discovers conversations "
                    "while none are chosen, so the list fills itself in."
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
        """Yield messages above the cursor, from allowed chats, inside the window.

        ROWID is monotonic in the Messages store — rows are appended, never renumbered —
        so the highest one seen is a complete watermark. `since=None` would be a full
        scan; `lookback_days` is what keeps that from meaning "the entire archive".

        The watermark advances past rows the allowlist rejects. It has to: those rows are
        a settled decision, and leaving the cursor behind them would make every later run
        re-read and re-reject the same messages forever.
        """
        self.excluded = 0
        self.excluded_by_rule = {}
        self.seen_chats = self.discover()

        watermark = _parse(since)
        highest = watermark

        with closing(self._connect()) as conn:
            window = f"-{self.lookback_days} days"
            for row in conn.execute(_QUERY, (watermark, NANOSECOND_THRESHOLD, window)):
                rowid = int(row["rowid"])
                # Advance past skipped rows too. A NULL-text row that never moved the
                # watermark would be re-read on every run forever.
                highest = max(highest, rowid)
                if not self._allowed(row):
                    continue
                item = self._to_item(row)
                if item is not None:
                    yield item

        self.cursor = str(highest)

    def _allowed(self, row: sqlite3.Row) -> bool:
        """Is this message in a conversation the owner named?

        A group is matched by its display name. A one-to-one has no display name in the
        Messages store, so it is matched by the other party's handle — the phone number
        or address the allowlist entry has to spell out.
        """
        chat = row["chat_name"] or None
        handle = row["handle"] or ""
        if self.allowlist.allows(title=chat, participants=[handle] if handle else []):
            return True
        self.excluded += 1
        rule = "allowlist"
        self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + 1
        return False

    def discover(self) -> dict[str, Sighting]:
        """Every conversation in the lookback window, with how much it said.

        Independent of the cursor, and that is the whole point. The cursor exists so a
        run does not re-read messages it has already stored; discovery answers a
        different question — *what conversations exist for the owner to decide about* —
        and that answer does not change when the messages have already been read. Tying
        it to the cursor produced a deadlock: nothing was monitored, so the page had to
        fill itself from sightings, but sightings only came from rows above a watermark
        that was already at the end of the store, so the page stayed empty and nothing
        could ever be chosen.

        One grouped query, no bodies read and nothing decoded — the counterparty handle
        and the group name are all a decision needs, and they are the only two things
        `_allowed` matches on, so a button on the page cannot turn on something the
        connector then fails to recognise.
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(_DISCOVER_QUERY, (f"-{self.lookback_days} days",)).fetchall()
        seen: dict[str, Sighting] = {}
        for row in rows:
            key = row["name"]
            if not key:
                continue
            seen[str(key)] = Sighting(
                key=str(key),
                display_name=str(key),
                kind="group" if row["is_group"] else "dm",
                messages=int(row["n"]),
            )
        return seen

    def _connect(self) -> sqlite3.Connection:
        # mode=ro, NOT immutable=1 — chat.db is WAL and immutable skips the -wal
        # file, making reads silently stale (or "no such table") while Messages.app
        # is open. The busy timeout rides out its checkpoint writes instead.
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 2000")
        return conn

    def _to_item(self, row: sqlite3.Row) -> SourceItem | None:
        if int(row["associated_message_type"] or 0) in TAPBACK_RANGE:
            # A tapback is a reaction to another message, not a message. Its text reads
            # like one, which is exactly why it has to be dropped before anything else.
            return None

        handle = (row["handle"] or "").strip()
        if not handle:
            # No handle means a system row — a group rename, someone leaving a thread.
            # There is no counterparty and no content worth a model call.
            return None

        text = _body_text(row)
        if not text:
            # Genuinely empty, or a blob that would not decode. See the docstring.
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


def _body_text(row: sqlite3.Row) -> str:
    """`message.text` when it is there, the decoded `attributedBody` when it is not."""
    text = (row["text"] or "").strip()
    if text:
        return text
    blob = row["attributed_body"]
    if isinstance(blob, memoryview):
        blob = blob.tobytes()
    if not isinstance(blob, bytes):
        return ""
    return _typedstream.extract_text(blob) or ""


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


# ── narrowing what is stored, after the fact ────────────────────────────────

#: Everything that points at a source_item, child-first. Derived from the schema rather
#: than remembered: `grep 'REFERENCES source_item' specs/schema.sql`. A table added later
#: and left out here would make the prune fail its foreign key rather than delete
#: silently, which is the safe direction, but the list is checked by a test so the
#: failure arrives in CI instead.
DEPENDENTS = (
    "engagement_evidence",
    "commitment_evidence",
    "commitment_recheck",
    "engagement",
    "commitment",
    "checkpoint",
    "fact",
    "model_batch_item",
    # A touchpoint's source item is always 'manual' and can never be an iMessage row, so
    # this line deletes nothing today. It is here because the list is about the schema,
    # not about which rows happen to match: leaving it out arms a foreign-key failure for
    # whoever later prunes a source that a touch could cite.
    "touchpoint",
    # Migration 0030. Only a windowed connector writes these and iMessage is not one, so
    # like `touchpoint` this deletes nothing today and is here for the same reason.
    "source_item_retraction",
    # Migration 0031, and the third of the same kind: only Canvas writes an assignment
    # row, so no iMessage prune will ever match one. Its `assignment_material` children
    # go with it through ON DELETE CASCADE, which holds because `db.connect` turns
    # `PRAGMA foreign_keys` on.
    "assignment",
)


@dataclass
class PruneReport:
    scanned: int = 0
    removed: int = 0
    kept: int = 0
    by_chat: dict[str, int] = field(default_factory=dict)


def prune(
    conn: sqlite3.Connection,
    allowlist: Allowlist,
    *,
    lookback_days: int,
    today: date | None = None,
    dry_run: bool = True,
) -> PruneReport:
    """Remove stored iMessage items the current allowlist and window no longer admit.

    docs/03 keeps raw items forever and migration 0005 enforces it, with one sanctioned
    exception: the docs/08 boundary purge, for content the owner has decided this system
    may not hold. Narrowing an allowlist is that same act — the rule about what may be
    stored got tighter, and rows captured under the looser one have to follow, or the
    setting is a promise about the future only.

    Defaults to a dry run. Deleting someone's messages on the strength of a config value
    they just typed should require saying so twice.
    """
    today = today or date.today()
    floor = (today - timedelta(days=lookback_days)).isoformat()
    report = PruneReport()

    doomed: list[int] = []
    for row in conn.execute(
        "SELECT id, occurred_at, raw_json FROM source_item WHERE user_id = 1 AND source = ?",
        ("imessage",),
    ):
        report.scanned += 1
        try:
            payload = json.loads(str(row["raw_json"] or "{}"))
        except ValueError:
            payload = {}
        chat = str(payload.get("chat") or "")
        handle = str(payload.get("handle") or "")
        allowed = allowlist.allows(
            title=chat or None, participants=[handle] if handle else []
        )
        # The window is compared on the local date prefix, like every other reader of a
        # stored timestamp in this codebase: occurred_at carries the sender's own offset.
        in_window = str(row["occurred_at"] or "")[:10] >= floor
        if allowed and in_window:
            report.kept += 1
            continue
        doomed.append(int(row["id"]))
        label = chat or handle or "(unknown)"
        report.by_chat[label] = report.by_chat.get(label, 0) + 1

    report.removed = len(doomed)
    if dry_run or not doomed:
        return report

    marks = ",".join("?" * len(doomed))
    conn.execute("BEGIN")
    try:
        # The 0005 delete guard, opened and closed inside one transaction exactly as the
        # boundary purge does it — a crash rolls the gate closed with everything else.
        conn.execute("UPDATE purge_gate SET open = 1 WHERE id = 1")
        for table in DEPENDENTS:
            conn.execute(f"DELETE FROM {table} WHERE source_item_id IN ({marks})", doomed)
        conn.execute(f"DELETE FROM source_item WHERE id IN ({marks})", doomed)
        conn.execute("UPDATE purge_gate SET open = 0 WHERE id = 1")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return report
