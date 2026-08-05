"""Who a phone number belongs to. Phase 13.

The Conversations page asked the owner to decide whether to read `+14802411748`, which
is not a question anybody can answer. Forty-three of forty-seven conversations sat
undecided behind that, and the eleven busiest were bare numbers — roughly six hundred
messages that never reached the ledger because the consent prompt would not say who was
talking.

**A contact is reference data, not a source item.** It is not an event, it carries no
commitment, and nothing about it resolves against a timestamp — so it does not belong in
the immutable capture table (docs/03, CLAUDE.md rule "raw source items are immutable and
kept forever", which is a promise about evidence). It belongs in `entity`, which already
exists for exactly this: a person, their canonical name, and `aliases_json` — the list
`Ledger.resolve_entity` already searches to fold "Dave" and drodriguez@… into one row.
Contacts fills that list with the identifiers the extractor never sees, so the same
person the owner texts and the person the owner emails are one row rather than two.

Three rules hold this together:

  1. **One canonical form, computed in one place** (`canonical`). The same person is
     `+14802411748`, `(480) 241-1748`, `480-241-1748` and `14802411748`; comparison
     happens on E.164-ish digits and nothing else compares raw strings.
  2. **An import adds identifiers and never renames.** `entity` rows are hand-editable
     through the People page, so a re-run that rewrote `canonical_name` would silently
     undo the owner's correction. Aliases are unioned; a run with nothing new to add
     performs zero writes (rule 3).
  3. **Ambiguity shows the raw identifier.** A number claimed by two contacts, or one
     that already resolves to two entities, resolves to nothing — a bare number the
     owner can look up beats a confident wrong name.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from backglass.db import now_iso
from backglass.ledger import USER_ID

_NON_DIGIT = re.compile(r"\D")

#: The country code assumed for a phone number written without one. Every bare number in
#: the owner's address book is North American (area codes 480/602/623/…); the handful of
#: Indian numbers are stored with their `+91`, which is why a default this crude is safe
#: here. It is only ever a *comparison* form: guessing wrong makes a contact fail to
#: resolve — the conversation keeps showing its raw number — rather than attaching
#: somebody else's name to it.
_DEFAULT_CALLING_CODE = "1"

#: Below this, digits are not a phone number: a short code (66960), a year in a group
#: name, a house number. They resolve to nothing rather than to a bad key.
_MIN_PHONE_DIGITS = 7


def canonical(identifier: str | None) -> str | None:
    """One comparable form for a phone number or an email address, or None.

    None means "this is not an identifier" — a group chat called `topgolf`, a five-digit
    short code — and callers treat that as *do not attempt resolution*, never as a miss.
    """
    raw = (identifier or "").strip()
    if not raw:
        return None
    if "@" in raw:
        return raw.casefold()
    digits = _NON_DIGIT.sub("", raw)
    if not digits:
        return None
    if raw.startswith("+"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+{_DEFAULT_CALLING_CODE}{digits}"
    if len(digits) == 11 and digits.startswith(_DEFAULT_CALLING_CODE):
        return f"+{digits}"
    if len(digits) < _MIN_PHONE_DIGITS:
        return None
    return f"+{digits}"


@dataclass(frozen=True)
class Contact:
    """One address-book card, already stripped to the parts that identify a person."""

    uid: str
    name: str
    #: Phone numbers and email addresses exactly as the address book wrote them.
    identifiers: tuple[str, ...] = ()
    is_org: bool = False

    @property
    def keys(self) -> list[str]:
        """Canonical identifiers, deduplicated, order preserved."""
        seen: dict[str, None] = {}
        for value in self.identifiers:
            key = canonical(value)
            if key:
                seen[key] = None
        return list(seen)


@dataclass
class ImportReport:
    read: int = 0
    entities_created: int = 0
    entities_updated: int = 0
    aliases_added: int = 0
    #: Canonical identifiers claimed by more than one card, and cards whose identifiers
    #: already point at more than one entity. Both are skipped, and named so the owner
    #: can merge on the People page rather than wonder why a number stayed bare.
    ambiguous: list[str] = field(default_factory=list)
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)

    @property
    def writes(self) -> int:
        return self.entities_created + self.entities_updated


@dataclass(frozen=True)
class Resolution:
    """What one conversation key turned out to be."""

    key: str
    name: str | None = None
    #: True when the key matched more than one entity. Distinguished from a plain miss
    #: because the page can say "two people claim this" rather than nothing.
    ambiguous: bool = False


def import_contacts(
    conn: sqlite3.Connection, contacts: Sequence[Contact], *, dry_run: bool = False
) -> ImportReport:
    """Fold an address book into `entity`. Idempotent; never renames.

    Matching order is identifier, then exact name, then create — the same order
    `Ledger.resolve_entity` uses, and for the same reason: an address is a fact and a
    display name is a guess, so the fact goes first. A card whose identifiers are all
    ambiguous is skipped entirely; writing it would mean choosing which of two people a
    number belongs to, which is the one thing this module must not do.
    """
    report = ImportReport(read=len(contacts))

    claims: dict[str, set[str]] = {}
    for contact in contacts:
        if not contact.name.strip():
            continue
        for key in contact.keys:
            claims.setdefault(key, set()).add(contact.name.strip().casefold())
    contested = {key for key, names in claims.items() if len(names) > 1}
    report.ambiguous.extend(sorted(contested))

    for contact in contacts:
        if not contact.name.strip():
            continue
        keys = [key for key in contact.keys if key not in contested]
        if not keys:
            continue

        matches = _entities_for(conn, keys)
        if len(matches) > 1:
            # Two entities already hold these identifiers between them. Merging them is
            # the owner's call (People → merge); guessing here would move an alias off a
            # row somebody may have curated.
            report.ambiguous.append(contact.name.strip())
            continue

        entity_id = matches[0] if matches else _entity_by_name(conn, contact.name)
        if entity_id is None:
            report.entities_created += 1
            report.aliases_added += len(keys)
            if not dry_run:
                conn.execute(
                    "INSERT INTO entity (user_id, kind, canonical_name, aliases_json,"
                    " updated_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        USER_ID,
                        "org" if contact.is_org else "person",
                        contact.name.strip(),
                        json.dumps(keys),
                        now_iso(),
                    ),
                )
            continue

        row = conn.execute(
            "SELECT aliases_json FROM entity WHERE id = ? AND user_id = ?",
            (entity_id, USER_ID),
        ).fetchone()
        existing = list(json.loads(row["aliases_json"] or "[]"))
        lowered = {str(alias).casefold() for alias in existing}
        fresh = [key for key in keys if key.casefold() not in lowered]
        if not fresh:
            # Rule 3: the second run of an unchanged address book writes nothing.
            continue
        report.entities_updated += 1
        report.aliases_added += len(fresh)
        if not dry_run:
            # aliases_json only. canonical_name, role, org, notes and tags are the
            # owner's, and a nightly sync does not get to overwrite what they typed.
            conn.execute(
                "UPDATE entity SET aliases_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps([*existing, *fresh]), now_iso(), entity_id),
            )
    return report


def resolve(
    conn: sqlite3.Connection, keys: Iterable[str]
) -> dict[str, Resolution]:
    """Name the people behind a batch of conversation keys.

    One query for the whole batch: this runs on every render of the Conversations page,
    and a per-row lookup over `json_each` would be forty-seven of them.
    """
    wanted: dict[str, str] = {}
    for key in keys:
        canonical_key = canonical(key)
        if canonical_key:
            wanted[key] = canonical_key
    if not wanted:
        return {}

    placeholders = ",".join("?" * len(set(wanted.values())))
    rows = conn.execute(
        "SELECT e.id AS id, e.canonical_name AS name, lower(a.value) AS alias"
        " FROM entity e, json_each(e.aliases_json) a"
        f" WHERE e.user_id = ? AND lower(a.value) IN ({placeholders})",
        (USER_ID, *sorted(set(wanted.values()))),
    ).fetchall()

    by_alias: dict[str, dict[int, str]] = {}
    for row in rows:
        by_alias.setdefault(str(row["alias"]), {})[int(row["id"])] = str(row["name"])

    out: dict[str, Resolution] = {}
    for key, canonical_key in wanted.items():
        hits = by_alias.get(canonical_key, {})
        if len(hits) == 1:
            out[key] = Resolution(key=key, name=next(iter(hits.values())))
        elif len(hits) > 1:
            out[key] = Resolution(key=key, ambiguous=True)
        else:
            out[key] = Resolution(key=key)
    return out


def _entities_for(conn: sqlite3.Connection, keys: Sequence[str]) -> list[int]:
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        "SELECT DISTINCT e.id AS id FROM entity e, json_each(e.aliases_json) a"
        f" WHERE e.user_id = ? AND lower(a.value) IN ({placeholders})",
        (USER_ID, *[key.casefold() for key in keys]),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _entity_by_name(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM entity WHERE user_id = ? AND kind IN ('person', 'org')"
        " AND lower(canonical_name) = ?",
        (USER_ID, name.strip().casefold()),
    ).fetchone()
    return int(row["id"]) if row else None
