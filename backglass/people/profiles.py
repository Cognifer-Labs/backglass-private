"""Search, profile, and timeline reads. Phase 6.

Every timeline row and open commitment carries source_item columns, because a
profile is a view over evidence, not a CRM record with a memory of its own.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from backglass.db import query
from backglass.ledger import USER_ID


def _like(term: str | None) -> str | None:
    if term is None or not term.strip():
        return None
    return f"%{term.strip().lower()}%"


#: Name tokens that mark a service desk, portal, or institution rather than a
#: human. Extraction still writes kind='person' for every counterparty (it cannot
#: know better); an owner `org` tag now writes through to entity.kind, and the
#: resolver matches both kinds, so a flipped row keeps resolving.
_ORG_TOKENS = frozenset(
    {"portal", "housing", "transportation", "office", "services", "admissions",
     "registrar", "financial", "aid", "loan", "loans", "bank", "university",
     "college", "department", "dept", "program", "support", "billing"}
)
#: Role words that mean "an unnamed human in a role", which outranks org tokens —
#: "college counselor" is a person you talk to, not a service you log into.
_ROLE_TOKENS = frozenset(
    {"coordinator", "counselor", "advisor", "adviser", "manager", "professor",
     "instructor", "teacher", "recruiter", "agent"}
)


def org_like(record: dict[str, Any]) -> bool:
    """Does this profile read as an org/service rather than a person?

    Persisted kind first: a row flipped to kind='org' is decided. Then the owner
    tag override, then: a curated role/org means a person; a role word in the name
    means an unnamed person; an org token or an all-caps acronym (NASA, ACME)
    means a service. Misfiles are harmless — the row renders identically, only the
    display group moves — and taggable.
    """
    if record.get("kind") == "org":
        return True
    tags = {str(t).lower() for t in record.get("tags") or []}
    if "org" in tags:
        return True
    if "person" in tags:
        return False
    if record.get("role"):
        return False
    words = str(record.get("canonical_name") or "").replace("/", " ").split()
    lowered = {w.strip(".,").lower() for w in words}
    if lowered & _ROLE_TOKENS:
        return False
    if lowered & _ORG_TOKENS:
        return True
    return any(len(w) >= 2 and w.isalpha() and w.isupper() for w in words)


def search(
    conn: sqlite3.Connection, *, q: str | None = None, tag: str | None = None
) -> list[dict[str, Any]]:
    """LIKE over name, aliases, role, org, tags. Empty query lists everyone."""
    tag_like = f'%"{tag.strip().lower()}"%' if tag and tag.strip() else None
    rows = conn.execute(
        query("people_search"),
        {"user_id": USER_ID, "q": _like(q), "tag": tag_like},
    ).fetchall()
    out = []
    for row in rows:
        record = dict(row)
        record["tags"] = json.loads(record.pop("tags_json") or "[]")
        record["aliases"] = json.loads(record.pop("aliases_json") or "[]")
        out.append(record)
    return out


def profile(conn: sqlite3.Connection, entity_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM entity WHERE id = ? AND user_id = ?", (entity_id, USER_ID)
    ).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["tags"] = json.loads(record.pop("tags_json") or "[]")
    record["aliases"] = json.loads(record.pop("aliases_json") or "[]")
    return record


def timeline(conn: sqlite3.Connection, entity_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            query("people_timeline"), {"user_id": USER_ID, "entity_id": entity_id}
        )
    ]


def open_commitments(conn: sqlite3.Connection, entity_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            query("people_open_commitments"), {"user_id": USER_ID, "entity_id": entity_id}
        )
    ]
