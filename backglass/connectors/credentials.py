"""Per-source OAuth tokens and cursors, in the `credential` table.

docs/07 §Credentials: "Env vars hold only the client ID and secret per provider, which
are app-level rather than user-level." The rationale is that per-user OAuth is the entire
difference between a script and a product, and the table shape is identical either way.

docs/08 §General handling: tokens never appear in logs, in the SQLite file outside the
`credential` table, or in brief output. Nothing in this module returns a token to a
caller that does not need it, and `__repr__` is not defined on anything holding one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from backglass.db import now_iso

USER_ID = 1


@dataclass
class CredentialRow:
    source: str
    access_token: str | None
    refresh_token: str | None
    expires_at: str | None
    cursor: str | None
    scopes: str | None
    status: str
    last_error: str | None


def load(conn: sqlite3.Connection, source: str) -> CredentialRow | None:
    row = conn.execute(
        "SELECT * FROM credential WHERE user_id = ? AND source = ?", (USER_ID, source)
    ).fetchone()
    if row is None:
        return None
    return CredentialRow(
        source=str(row["source"]),
        access_token=row["access_token"],
        refresh_token=row["refresh_token"],
        expires_at=row["expires_at"],
        cursor=row["cursor"],
        scopes=row["scopes"],
        status=str(row["status"]),
        last_error=row["last_error"],
    )


def save_tokens(
    conn: sqlite3.Connection,
    source: str,
    *,
    access_token: str | None,
    refresh_token: str | None,
    expires_at: str | None,
    scopes: str | None,
) -> None:
    conn.execute(
        "INSERT INTO credential (user_id, source, access_token, refresh_token, expires_at, "
        "                        scopes, status, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'ok', ?) "
        "ON CONFLICT (user_id, source) DO UPDATE SET "
        "  access_token = excluded.access_token, "
        "  refresh_token = COALESCE(excluded.refresh_token, credential.refresh_token), "
        "  expires_at = excluded.expires_at, "
        "  scopes = excluded.scopes, "
        "  status = 'ok', last_error = NULL, updated_at = excluded.updated_at",
        (USER_ID, source, access_token, refresh_token, expires_at, scopes, now_iso()),
    )


def save_cursor(conn: sqlite3.Connection, source: str, cursor: str | None) -> None:
    """The cursor is what makes a routine run incremental. docs/07: never re-scan from zero."""
    conn.execute(
        "INSERT INTO credential (user_id, source, cursor, status, updated_at) "
        "VALUES (?, ?, ?, 'ok', ?) "
        "ON CONFLICT (user_id, source) DO UPDATE SET "
        "  cursor = excluded.cursor, updated_at = excluded.updated_at",
        (USER_ID, source, cursor, now_iso()),
    )


def mark_failed(conn: sqlite3.Connection, source: str, error: str) -> None:
    """docs/07 §Health: status stays 'failed' until a successful fetch.

    Auth expiry is a first-class visible state, not an exception in a log.
    """
    conn.execute(
        "INSERT INTO credential (user_id, source, status, last_error, updated_at) "
        "VALUES (?, ?, 'failed', ?, ?) "
        "ON CONFLICT (user_id, source) DO UPDATE SET "
        "  status = 'failed', last_error = excluded.last_error, "
        "  updated_at = excluded.updated_at",
        (USER_ID, source, error[:500], now_iso()),
    )


def set_enabled(conn: sqlite3.Connection, source: str, enabled: bool) -> None:
    """The pause switch. Upserts so a source can be disabled before its first sync
    ever runs — the row exists from this moment either way."""
    conn.execute(
        "INSERT INTO credential (user_id, source, status, enabled, updated_at) "
        "VALUES (?, ?, 'ok', ?, ?) "
        "ON CONFLICT (user_id, source) DO UPDATE SET "
        "  enabled = excluded.enabled, updated_at = excluded.updated_at",
        (USER_ID, source, int(enabled), now_iso()),
    )


def disabled_sources(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["source"])
        for row in conn.execute(
            "SELECT source FROM credential WHERE user_id = ? AND enabled = 0", (USER_ID,)
        )
    }
