"""Which conversations the ledger may read, and which the owner has not answered yet.

One module because there are three callers with one question between them: the connectors
ask "may I read this", `sync` records what was seen, and the web layer shows what is
waiting to be decided. Keeping the answer in one place is what stops a chat being
monitored on one surface and invisible on another.

The decision lives in the `monitored_chat` table rather than in `IMESSAGE_CHATS` /
`INSTAGRAM_CHATS`, for the reason migration 0015 gives: an allowlist typed into `.env`
from memory fails silently when it is spelled wrong, and it can only ever describe chats
the owner already knows about. The env vars still work — they are seeded into the table as
`monitor` — so nothing breaks the moment this lands.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field

from backglass.connectors.allowlist import Allowlist, normalise
from backglass.db import now_iso
from backglass.ledger import USER_ID

MONITOR = "monitor"
IGNORE = "ignore"


@dataclass(frozen=True)
class Sighting:
    """A conversation a connector saw during one fetch, allowed or not."""

    key: str
    display_name: str
    kind: str = "group"
    participants: int | None = None
    messages: int = 1


@dataclass
class Chat:
    """A row of the table, as the web layer wants to read it."""

    id: int
    source: str
    key: str
    display_name: str
    kind: str
    decision: str | None
    participants: int | None
    messages_seen: int
    last_seen_at: str

    @property
    def undecided(self) -> bool:
        return self.decision is None


@dataclass
class SightingReport:
    recorded: int = 0
    new: int = 0
    names: list[str] = field(default_factory=list)


def allowlist_for(
    conn: sqlite3.Connection, source: str, fallback: Sequence[str] = ()
) -> Allowlist:
    """What `source` is allowed to read.

    `fallback` is the connector's env setting. It is unioned rather than replaced, so a
    machine whose `.env` still carries the list keeps working before anything has been
    decided in the UI — and `seed_from_env` turns those entries into real rows on the
    first sync, after which the table is the only thing that matters.
    """
    rows = conn.execute(
        "SELECT key FROM monitored_chat WHERE user_id = ? AND source = ? AND decision = ?",
        (USER_ID, source, MONITOR),
    ).fetchall()
    return Allowlist([*(str(row["key"]) for row in rows), *fallback])


def seed_from_env(conn: sqlite3.Connection, source: str, entries: Sequence[str]) -> int:
    """Turn an existing env allowlist into decided rows. Returns how many were created.

    Idempotent, and deliberately never overwrites: if the owner has since pressed Ignore
    on a chat that is still named in `.env`, the button wins. A setting is a stale
    artefact of how this used to work; a click is a decision made now.
    """
    created = 0
    for entry in entries:
        if not entry.strip():
            continue
        cursor = conn.execute(
            "INSERT INTO monitored_chat"
            " (user_id, source, key, display_name, kind, decision,"
            "  messages_seen, first_seen_at, last_seen_at, decided_at)"
            " VALUES (?, ?, ?, ?, 'group', ?, 0, ?, ?, ?)"
            " ON CONFLICT (user_id, source, key) DO NOTHING",
            (
                USER_ID, source, normalise(entry), entry, MONITOR,
                now_iso(), now_iso(), now_iso(),
            ),
        )
        created += 1 if cursor.rowcount == 1 else 0
    return created


def record(
    conn: sqlite3.Connection, source: str, sightings: Sequence[Sighting]
) -> SightingReport:
    """Write what a connector saw. New conversations land undecided.

    Undecided is the point: it is not "off", it is "the owner has not been asked yet", and
    it is what puts the chat on the prompt. Nothing is ever monitored by default — a new
    conversation is a question, because consent to read one group says nothing about the
    next.

    `messages_seen` accumulates and `last_seen_at` moves, so a chat that has gone quiet
    sorts below one that is active and the page can say which is which.
    """
    report = SightingReport()
    for sighting in sightings:
        key = normalise(sighting.key)
        if not key:
            continue
        cursor = conn.execute(
            "INSERT INTO monitored_chat"
            " (user_id, source, key, display_name, kind, participants,"
            "  messages_seen, first_seen_at, last_seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (user_id, source, key) DO UPDATE SET"
            "   messages_seen = messages_seen + excluded.messages_seen,"
            "   last_seen_at = excluded.last_seen_at,"
            "   display_name = COALESCE(excluded.display_name, display_name),"
            "   participants = COALESCE(excluded.participants, participants)",
            (
                USER_ID,
                source,
                key,
                sighting.display_name or sighting.key,
                sighting.kind,
                sighting.participants,
                sighting.messages,
                now_iso(),
                now_iso(),
            ),
        )
        report.recorded += 1
        if cursor.rowcount == 1:
            report.new += 1
            report.names.append(sighting.display_name or sighting.key)
    return report


def decide(conn: sqlite3.Connection, chat_id: int, decision: str) -> bool:
    """Monitor or ignore one conversation. Returns False if nothing changed.

    `ignore` is stored rather than treated as absence, so the same chat is never asked
    about twice — without it every sync would re-raise every group the owner has already
    declined, and a prompt that repeats itself is a prompt people stop reading.
    """
    if decision not in (MONITOR, IGNORE):
        raise ValueError(f"unknown decision {decision!r}")
    cursor = conn.execute(
        "UPDATE monitored_chat SET decision = ?, decided_at = ?"
        " WHERE user_id = ? AND id = ? AND (decision IS NOT ? OR decision IS NULL)",
        (decision, now_iso(), USER_ID, chat_id, decision),
    )
    return cursor.rowcount == 1


def listing(conn: sqlite3.Connection, source: str | None = None) -> list[Chat]:
    """Every known conversation, undecided first, then busiest."""
    sql = (
        "SELECT id, source, key, display_name, kind, decision, participants,"
        " messages_seen, last_seen_at FROM monitored_chat WHERE user_id = ?"
    )
    params: list[object] = [USER_ID]
    if source:
        sql += " AND source = ?"
        params.append(source)
    sql += " ORDER BY (decision IS NOT NULL), messages_seen DESC, last_seen_at DESC"
    return [
        Chat(
            id=int(row["id"]),
            source=str(row["source"]),
            key=str(row["key"]),
            display_name=str(row["display_name"] or row["key"]),
            kind=str(row["kind"]),
            decision=row["decision"],
            participants=row["participants"],
            messages_seen=int(row["messages_seen"]),
            last_seen_at=str(row["last_seen_at"]),
        )
        for row in conn.execute(sql, params)
    ]


def undecided(conn: sqlite3.Connection) -> list[Chat]:
    return [chat for chat in listing(conn) if chat.undecided]
