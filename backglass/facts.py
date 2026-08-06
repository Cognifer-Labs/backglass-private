"""The personal knowledge base. Phase 12.

Small durable claims about the owner — which dorm, which programs, what was declined —
so nothing (the owner, an assistant, a future extractor) has to re-derive them from
mail. Three rules, all inherited from the ledger:

- Every fact says where it came from (`source`, `note`, optional `source_item_id`).
- Memory changes by supersession, never by UPDATE — history survives its corrections.
- Retraction is a status, not a DELETE.

Subjects are freeform kebab lanes (identity, housing, premed, side-project, ...). The
(subject, key) pair is the identity a new value supersedes; the Memory page groups by
subject so near-duplicate keys stay visible rather than silently forking.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from backglass.config import Settings
from backglass.ledger import USER_ID
from backglass.plan import timezones

SOURCES = ("manual", "extraction", "assistant")


class FactError(ValueError):
    pass


@dataclass(frozen=True)
class Fact:
    fact_id: int
    subject: str
    key: str
    value: str
    note: str | None
    source: str
    created_at: str


def remember(
    conn: sqlite3.Connection,
    settings: Settings,
    subject: str,
    key: str,
    value: str,
    *,
    note: str | None = None,
    source: str = "manual",
    source_item_id: int | None = None,
) -> int:
    """Store a fact; the previous active fact for this (subject, key) is superseded."""
    subject, key, value = subject.strip().lower(), key.strip().lower(), value.strip()
    if not subject or not key or not value:
        raise FactError("a fact needs a subject, a key and a value")
    if source not in SOURCES:
        raise FactError(f"unknown fact source {source!r}; expected one of {SOURCES}")

    cur = conn.execute(
        "INSERT INTO fact (user_id, subject, key, value, note, source, source_item_id,"
        " status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)",
        (
            USER_ID,
            subject,
            key,
            value,
            (note or "").strip() or None,
            source,
            source_item_id,
            timezones.local_now_iso(settings),
        ),
    )
    new_id = int(cur.lastrowid or 0)
    conn.execute(
        "UPDATE fact SET status = 'superseded', superseded_by = ? "
        "WHERE user_id = ? AND subject = ? AND key = ? AND status = 'active' AND id != ?",
        (new_id, USER_ID, subject, key, new_id),
    )
    return new_id


def recall(conn: sqlite3.Connection, subject: str | None = None) -> list[Fact]:
    """Active facts, ordered by subject then key — the whole memory, or one lane."""
    where = "user_id = ? AND status = 'active'"
    params: list[Any] = [USER_ID]
    if subject:
        where += " AND subject = ?"
        params.append(subject.strip().lower())
    rows = conn.execute(
        f"SELECT id, subject, key, value, note, source, created_at FROM fact "
        f"WHERE {where} ORDER BY subject, key",
        params,
    ).fetchall()
    return [
        Fact(
            fact_id=int(r["id"]),
            subject=str(r["subject"]),
            key=str(r["key"]),
            value=str(r["value"]),
            note=r["note"],
            source=str(r["source"]),
            created_at=str(r["created_at"]),
        )
        for r in rows
    ]


def forget(conn: sqlite3.Connection, fact_id: int) -> None:
    """Retract — the row survives; only its claim is withdrawn."""
    cur = conn.execute(
        "UPDATE fact SET status = 'retracted' "
        "WHERE id = ? AND user_id = ? AND status = 'active'",
        (fact_id, USER_ID),
    )
    if cur.rowcount == 0:
        raise FactError(f"no active fact {fact_id}")


def export_markdown(conn: sqlite3.Connection) -> str:
    """The whole active memory as one compact markdown doc.

    This is what an assistant loads instead of searching mail: every line carries its
    date and source so a stale claim is visibly stale rather than silently trusted.
    Standing decisions ride along — "Sallie Mae: not doing it" is exactly the durable
    owner fact this export exists to save someone from re-deriving, and an assistant
    that knows the memory but not the decisions will cheerfully suggest the thing the
    owner already declined.
    """
    from backglass import decisions as decisions_mod

    facts = recall(conn)
    standing = decisions_mod.active(conn)
    if not facts and not standing:
        return "# Owner memory\n\n(empty)\n"
    lines = ["# Owner memory", ""]
    current = None
    for f in facts:
        if f.subject != current:
            if current is not None:
                lines.append("")
            current = f.subject
            lines += [f"## {current}", ""]
        line = f"- **{f.key}**: {f.value}"
        if f.note:
            line += f" — {f.note}"
        line += f" _({f.source} · {f.created_at[:10]})_"
        lines.append(line)
    if standing:
        if facts:
            lines.append("")
        lines += ["## standing decisions", ""]
        for d in standing:
            line = f"- **{d.title}**: {d.choice}"
            if d.reasoning:
                line += f" — {d.reasoning}"
            line += f" _(decided {d.decided_at[:10]})_"
            lines.append(line)
    lines.append("")
    return "\n".join(lines)
