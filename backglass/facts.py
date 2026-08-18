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

import re
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
    confidence: float | None = None,
) -> int:
    """Store a fact; the previous active fact for this (subject, key) is superseded.

    `confidence` is the model's number and only an extraction has one — NULL for a
    hand-typed fact, which is not "1.0", it is simply not an extraction (0026)."""
    subject, key, value = subject.strip().lower(), key.strip().lower(), value.strip()
    if not subject or not key or not value:
        raise FactError("a fact needs a subject, a key and a value")
    if source not in SOURCES:
        raise FactError(f"unknown fact source {source!r}; expected one of {SOURCES}")

    cur = conn.execute(
        "INSERT INTO fact (user_id, subject, key, value, note, source, source_item_id,"
        " confidence, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
        (
            USER_ID,
            subject,
            key,
            value,
            (note or "").strip() or None,
            source,
            source_item_id,
            confidence,
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


#: Lanes the extractor may write into — the closed list the prompt states. A model
#: inventing lanes forks the knowledge base into near-duplicates no reader groups
#: together; anything that fits nowhere lands in "other", visibly.
EXTRACTABLE_SUBJECTS = (
    "identity", "education", "housing", "preferences", "people", "family",
    "work", "health", "orgtruth", "other",
)

#: What an extracted fact must clear to land active without the owner's click. Below
#: it (or with an unverifiable citation) the candidate waits as `proposed` — visible
#: on the Memory page, invisible to owner_context, harmless until accepted.
AUTO_ACCEPT_CONFIDENCE = 0.8


def apply_extracted(
    conn: sqlite3.Connection,
    settings: Settings,
    candidates: list[Any],
    *,
    source_item_id: int,
    body_text: str,
) -> int:
    """Write extraction v10's fact candidates through the poison gate. Returns writes.

    The gate, in order:

    1. **Unknown lane → proposed.** The prompt states the closed list; a candidate
       outside it is a candidate that did not follow instructions, which is itself
       evidence about the rest of it.
    2. **Citation check.** `evidence` must appear verbatim in the source body
       (whitespace-normalized), the same rule recheck applies before closing a
       commitment. A model that cannot quote the sentence it read does not get to
       write memory.
    3. **Confidence.** At or above AUTO_ACCEPT_CONFIDENCE, and with 1–2 passed, the
       fact lands `active` through remember() — supersession included, provenance
       attached. Otherwise `proposed`.
    4. **Same-value re-emission is a no-write** (rule 3): a re-extraction that says
       what the ledger already says leaves no row behind, active or proposed.

    Why the asymmetry: a missing fact costs one re-read; a wrong active fact rides
    owner_context into every future model call and compounds. Proposed is the cheap
    failure mode, so everything doubtful lands there.
    """
    flat = " ".join(str(body_text or "").split()).lower()
    written = 0
    for cand in candidates:
        subject = str(cand.subject).strip().lower()
        key = str(cand.key).strip().lower()
        value = str(cand.value).strip()
        if not subject or not key or not value:
            continue

        current = conn.execute(
            "SELECT value FROM fact WHERE user_id = ? AND subject = ? AND key = ?"
            " AND status = 'active'",
            (USER_ID, subject, key),
        ).fetchone()
        if current is not None and str(current["value"]).strip() == value:
            continue  # rule 3: the ledger already says this
        pending = conn.execute(
            "SELECT 1 FROM fact WHERE user_id = ? AND subject = ? AND key = ?"
            " AND value = ? AND status = 'proposed'",
            (USER_ID, subject, key, value),
        ).fetchone()
        if pending is not None:
            continue  # already waiting on the owner; asking twice is nagging

        quote = " ".join(str(cand.evidence or "").split()).lower()
        cited = bool(quote) and quote in flat
        confident = float(cand.confidence) >= AUTO_ACCEPT_CONFIDENCE
        known_lane = subject in EXTRACTABLE_SUBJECTS

        if cited and confident and known_lane:
            remember(
                conn, settings, subject, key, value,
                note=str(cand.evidence).strip(),
                source="extraction",
                source_item_id=source_item_id,
                confidence=float(cand.confidence),
            )
        else:
            conn.execute(
                "INSERT INTO fact (user_id, subject, key, value, note, source,"
                " source_item_id, confidence, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'extraction', ?, ?, 'proposed', ?)",
                (
                    USER_ID, subject, key, value,
                    str(cand.evidence or "").strip() or None,
                    source_item_id, float(cand.confidence),
                    timezones.local_now_iso(settings),
                ),
            )
        written += 1
    return written


@dataclass(frozen=True)
class Proposed:
    fact_id: int
    subject: str
    key: str
    value: str
    note: str | None
    confidence: float | None
    source_item_id: int | None
    created_at: str


def proposed(conn: sqlite3.Connection) -> list[Proposed]:
    """Candidates waiting on the owner, oldest first — the review queue for memory."""
    rows = conn.execute(
        "SELECT id, subject, key, value, note, confidence, source_item_id, created_at"
        " FROM fact WHERE user_id = ? AND status = 'proposed' ORDER BY id",
        (USER_ID,),
    ).fetchall()
    return [
        Proposed(
            fact_id=int(r["id"]),
            subject=str(r["subject"]),
            key=str(r["key"]),
            value=str(r["value"]),
            note=r["note"],
            confidence=r["confidence"],
            source_item_id=r["source_item_id"],
            created_at=str(r["created_at"]),
        )
        for r in rows
    ]


def accept(conn: sqlite3.Connection, settings: Settings, fact_id: int) -> int:
    """Owner accepts a proposed fact: it becomes active through the supersession path.

    Re-written via remember() rather than UPDATEd to active, so the previous active
    fact for the (subject, key) is superseded exactly as a CLI write would — one code
    path for "a fact becomes current", whoever triggers it. The proposed row itself is
    marked superseded by the accepted one: history survives, and re-accepting is inert.
    """
    row = conn.execute(
        "SELECT subject, key, value, note, source_item_id, confidence FROM fact"
        " WHERE id = ? AND user_id = ? AND status = 'proposed'",
        (fact_id, USER_ID),
    ).fetchone()
    if row is None:
        raise FactError(f"no proposed fact {fact_id}")
    new_id = remember(
        conn, settings, str(row["subject"]), str(row["key"]), str(row["value"]),
        note=row["note"], source="extraction",
        source_item_id=row["source_item_id"], confidence=row["confidence"],
    )
    conn.execute(
        "UPDATE fact SET status = 'superseded', superseded_by = ? WHERE id = ?",
        (new_id, fact_id),
    )
    return new_id


def reject(conn: sqlite3.Connection, fact_id: int) -> None:
    """Owner rejects a candidate — retracted, kept, and never re-proposed: the
    same-value guard in apply_extracted only checks active and proposed rows, so add
    nothing here; a rejected value CAN come back if a later message re-states it,
    because a fact wrong in June can be true in September."""
    cur = conn.execute(
        "UPDATE fact SET status = 'retracted' WHERE id = ? AND user_id = ?"
        " AND status = 'proposed'",
        (fact_id, USER_ID),
    )
    if cur.rowcount == 0:
        raise FactError(f"no proposed fact {fact_id}")


def forget(conn: sqlite3.Connection, fact_id: int) -> None:
    """Retract — the row survives; only its claim is withdrawn."""
    cur = conn.execute(
        "UPDATE fact SET status = 'retracted' "
        "WHERE id = ? AND user_id = ? AND status = 'active'",
        (fact_id, USER_ID),
    )
    if cur.rowcount == 0:
        raise FactError(f"no active fact {fact_id}")


#: How much of the knowledge base may ride on a triage call. Every token spent in
#: triage multiplies across the whole inbox (triage.md), so the block is capped rather
#: than trusted to stay small — twenty facts is nothing, five hundred would be a tax on
#: every item forever. Measured 2026-08-07: cost tracks OUTPUT, not input, so the
#: marginal cost of a few hundred characters of context is close to nothing; the cap is
#: there for the day that stops being true, not because it is expensive today.
OWNER_CONTEXT_CHARS = 900


def owner_context(conn: sqlite3.Connection, *, limit: int = OWNER_CONTEXT_CHARS) -> str:
    """The knowledge base as a block a prompt can carry, or "" when there is none.

    Triage's whole job is telling the owner's own obligations from broadcast noise, and
    until now it did that knowing nothing about the owner. It could not tell that a
    university's admissions mail is marketing to someone who has already enrolled
    somewhere, because it did not know they had.

    Two properties this has to keep. It is DETERMINISTIC — ordered by subject then key,
    so the same ledger renders the same block and a caching backend is not defeated by a
    dictionary's iteration order. And it is EMPTY when the ledger has no facts, which
    means a fresh install sends byte-for-byte the prompt it sent before this existed:
    nobody inherits a behaviour change they have no data for.

    Facts are stated, never instructions. The block says who the owner is; it does not
    say what to drop. triage.md's asymmetry — when in doubt, keep — is the model's rule
    and this must not read as permission to override it.
    """
    rows = sorted(recall(conn), key=lambda f: (f.subject, f.key))
    if not rows:
        return ""
    lines: list[str] = []
    used = 0
    for fact in rows:
        line = f"- {fact.subject}/{fact.key}: {fact.value}"
        if used + len(line) + 1 > limit:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return "ABOUT THE OWNER (facts they have recorded about themselves)\n" + "\n".join(lines)


def config_drift(conn: sqlite3.Connection, settings: Any) -> list[str]:
    """Where the knowledge base and the effective config disagree about the owner.

    Two of these facts restate values the pipeline actually runs on: `identity.emails`
    against `OWNER_EMAILS`, and `identity.timezones` against `DEFAULT_TZ`/`ALT_TZ`. They
    agree today. Nothing would notice if they stopped, and this repo has already learned
    what that costs — "a documented invariant with no test is a comment" (2026-08-02,
    after `user_id on every table` had been false for eight tables for months).

    Config is canonical, because it is what the code reads; the fact is the human-legible
    copy that carries the annotations config cannot ("(personal)", "when in India"), so
    it is checked rather than generated. Drift is reported in both directions: a
    configured address missing from the KB means the record of the owner is stale, and a
    KB address the config has never heard of means mail from it is not being recognised
    as the owner's own.
    """
    facts = {f.key: f.value for f in recall(conn, "identity")}
    out: list[str] = []

    emails = facts.get("emails")
    if emails is not None:
        text = emails.lower()
        for address in settings.owner_emails:
            if address.lower() not in text:
                out.append(f"OWNER_EMAILS has {address}, identity.emails does not")
        for token in re.findall(r"[\w.+-]+@[\w.-]+", text):
            if not any(token == a.lower() for a in settings.owner_emails):
                out.append(f"identity.emails has {token}, OWNER_EMAILS does not")

    zones = facts.get("timezones")
    if zones is not None:
        for label, zone in (("DEFAULT_TZ", settings.default_tz), ("ALT_TZ", settings.alt_tz)):
            if zone and zone not in zones:
                out.append(f"{label} is {zone}, identity.timezones does not mention it")
    return out


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
