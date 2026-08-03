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


def plans(
    conn: sqlite3.Connection, entity_id: int, today: str
) -> dict[str, list[dict[str, Any]]]:
    """This person's plans, split into what is still ahead and what already happened.

    `today` is passed in rather than read from a clock so the split is testable and so a
    caller rendering a page for a specific day gets that day's answer. The comparison is
    on the local date prefix, matching how the column is written and how every other
    reader of it works.
    """
    upcoming: list[dict[str, Any]] = []
    past: list[dict[str, Any]] = []
    for row in conn.execute(
        query("people_plans"), {"user_id": USER_ID, "entity_id": entity_id}
    ):
        record = dict(row)
        starts = str(record["starts_at"] or "")[:10]
        settled = str(record["status"]) in ("declined", "done")
        if starts and starts < today[:10] or settled:
            past.append(record)
        else:
            upcoming.append(record)
    upcoming.reverse()  # the query sorts newest-first; ahead of you reads soonest-first
    return {"upcoming": upcoming, "past": past}


def derived(
    conn: sqlite3.Connection, entity_id: int, today: str
) -> dict[str, Any]:
    """What the ledger knows about this person, as opposed to what the owner typed.

    Everything here is computed at read time from evidence already stored — no new
    column, no enrichment pass, nothing to fall out of date. That is the same choice
    `touch.py` makes, and it matters more here than it would in a CRM: a profile that
    caches a summary can be wrong in a way the underlying rows are not, and the one
    thing this system sells is that a claim can be checked.

    `lean` reads the relationship off how the two of them actually spend time, not off
    a label anyone applied: someone the owner only ever meets professionally reads
    professional even if they are also a friend, because that is what the evidence says.
    It is a description, never a filter — nothing is hidden on the strength of it.
    """
    # Two legs unioned rather than summed in SQL: a person can reach the owner through a
    # commitment, a plan, or both, and GROUP BY over the union would need the same
    # merging anyway. Done in Python where the tie-break is readable.
    merged: dict[str, int] = {}
    for row in conn.execute(
        "SELECT s.source AS source, COUNT(*) AS n FROM engagement_person p "
        "  JOIN engagement e ON e.id = p.engagement_id "
        "  JOIN source_item s ON s.id = e.source_item_id "
        " WHERE p.user_id = :user_id AND p.entity_id = :entity_id GROUP BY s.source "
        "UNION ALL "
        "SELECT s.source AS source, COUNT(*) AS n FROM commitment c "
        "  JOIN source_item s ON s.id = c.source_item_id "
        " WHERE c.user_id = :user_id AND c.counterparty_entity_id = :entity_id "
        " GROUP BY s.source",
        {"user_id": USER_ID, "entity_id": entity_id},
    ):
        source = str(row["source"])
        merged[source] = merged.get(source, 0) + int(row["n"])

    kinds = dict(
        (str(row["kind"]), int(row["n"]))
        for row in conn.execute(
            "SELECT e.kind, COUNT(*) AS n FROM engagement_person p "
            "JOIN engagement e ON e.id = p.engagement_id "
            "WHERE p.user_id = ? AND p.entity_id = ? GROUP BY e.kind",
            (USER_ID, entity_id),
        )
    )
    social, professional = kinds.get("social", 0), kinds.get("professional", 0)
    if social and professional:
        lean = "both"
    elif social:
        lean = "social"
    elif professional:
        lean = "professional"
    else:
        lean = None

    span = conn.execute(
        "SELECT MIN(occurred_at) AS first_at, MAX(occurred_at) AS last_at, COUNT(*) AS n "
        "FROM ("
        "  SELECT s.occurred_at FROM commitment c JOIN source_item s ON s.id = c.source_item_id"
        "   WHERE c.user_id = :user_id AND c.counterparty_entity_id = :entity_id"
        "  UNION ALL"
        "  SELECT s.occurred_at FROM engagement_person p"
        "   JOIN engagement e ON e.id = p.engagement_id"
        "   JOIN source_item s ON s.id = e.source_item_id"
        "   WHERE p.user_id = :user_id AND p.entity_id = :entity_id"
        ")",
        {"user_id": USER_ID, "entity_id": entity_id},
    ).fetchone()

    both = plans(conn, entity_id, today)
    return {
        "channels": [
            {"source": source, "count": count}
            for source, count in sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "lean": lean,
        "social_count": social,
        "professional_count": professional,
        "first_at": span["first_at"] if span else None,
        "last_at": span["last_at"] if span else None,
        "mentions": int(span["n"]) if span and span["n"] else 0,
        "upcoming": both["upcoming"],
        "past_plans": both["past"],
    }
