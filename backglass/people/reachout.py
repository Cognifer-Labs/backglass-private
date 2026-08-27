"""Warm-keeping email drafts for people the ledger has little or nothing on.

`touch.py` measures silence it has evidence for. Someone met once in person leaves no
evidence at all — no thread, no commitment, no calendar block — so the relationships
most likely to go cold are exactly the ones the warmth machinery is blindest to. What
closes that gap is not another detector; it is the next action, written.

Three properties, each of them a decision rather than a default:

  1. **No model call.** A template that fills named slots costs nothing, renders the
     same way twice, and calls no live API — so it can be tested against fixtures like
     any other read (docs/10 §Testing). The sentence that carries the relationship is
     the owner's `note`, in the owner's voice, and a model paraphrasing it would only
     make it sound like everyone else's outreach.
  2. **Zero ledger evidence is the primary case.** A name and a note render a complete,
     sendable email. Last touch, org and open commitments are enrichment when they
     exist, never a requirement — the in-person connection is who this is for.
  3. **Provenance sits beside the draft, never inside it** (CLAUDE.md rule 1). Every
     `Draft.evidence` line names the row, note or fact its slot came from. The email
     body itself contains nothing the owner did not type or that the entity row does
     not say, which is why it can be sent without checking anything.

Nothing here writes. Sending the draft is what produces evidence: the reply arrives
through apple-mail like any other item and reaches the ledger by the ordinary path.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import quote

from backglass.config import Settings
from backglass.ledger import USER_ID
from backglass.people import profiles, touch


class ReachoutError(ValueError):
    pass


@dataclass(frozen=True)
class Template:
    name: str
    #: Three or four words for the picker. A `<select>` is as wide as its widest
    #: option, so a sentence here is a control that overruns the panel — the same
    #: lesson the Decisions picker already carries in dashboard.css.
    label: str
    #: One line, for the CLI and the caption under the picker — when to reach for it.
    purpose: str
    subject: str
    #: Paragraphs. `{}`-slots are filled from `_slots`; a paragraph whose slots are all
    #: empty is dropped whole, which is how the same template survives a person the
    #: ledger has never seen.
    paragraphs: tuple[str, ...]


#: The templates. Deliberately three: the moment after meeting someone, the gap that
#: opened afterwards, and the one where there is something to ask for. More would be a
#: menu nobody reads, and the `note` is where the variation actually lives.
TEMPLATES: dict[str, Template] = {
    "thanks": Template(
        name="thanks",
        label="just met them",
        purpose="Just met them — thank them while the conversation is still fresh.",
        subject="Thank you for your time, {first}",
        paragraphs=(
            "Hi {first},",
            "Thank you for taking the time to talk{where}. {note}",
            "I won't take more of your time. I mainly wanted to say the conversation "
            "stuck with me, and that I'm glad we met.",
            "If it's ever useful to trade notes again, I'd welcome it.",
            "Best,\n{owner}{owner_email_line}",
        ),
    ),
    "warm": Template(
        name="warm",
        label="it has been a while",
        purpose="A gap has opened since you last spoke — reopen it with no ask attached.",
        subject="Hello from {owner_first}",
        paragraphs=(
            "Hi {first},",
            "It's been a while since we last spoke{gap} and you came to mind, so I "
            "thought I'd say hello.",
            "{note}",
            "No ask here at all. I'd just like to stay in touch. If you're ever up for "
            "a short call or a coffee, I'm around.",
            "Best,\n{owner}{owner_email_line}",
        ),
    ),
    "ask": Template(
        name="ask",
        label="one small ask",
        purpose="Reconnect with one specific, small, easy-to-refuse request.",
        subject="A quick question, {first}",
        paragraphs=(
            "Hi {first},",
            "I've thought back on our conversation{where} more than once since, and "
            "on what you're working on{at_org}.",
            "{note}",
            "If now isn't a good time, no need to reply. I'll assume it isn't and "
            "won't take it as anything else.",
            "Best,\n{owner}{owner_email_line}",
        ),
    ),
}


@dataclass(frozen=True)
class Draft:
    entity_id: int
    to_name: str
    #: The first address on the profile's alias list, or None. A draft with no address
    #: is still a complete draft — most in-person connections start that way.
    to_email: str | None
    template: str
    subject: str
    body: str
    #: One line per slot that was filled, naming where it came from. Rule 1.
    evidence: tuple[str, ...]

    def mailto(self) -> str | None:
        """An RFC 6068 link, or None when no address is known."""
        if not self.to_email:
            return None
        return (
            f"mailto:{quote(self.to_email)}"
            f"?subject={quote(self.subject)}&body={quote(self.body)}"
        )

    def as_text(self) -> str:
        to = self.to_email or self.to_name
        return f"To: {to}\nSubject: {self.subject}\n\n{self.body}"


def _first_name(full_name: str) -> str:
    """The name to open an email with.

    A profile's `canonical_name` can be a full name, a single name, or a service desk
    that was never a person. Taking the first whitespace-separated word is right for the
    first two and harmless for the third — the owner reads the draft before sending it,
    and a wrong salutation is visible in the first line rather than buried.
    """
    parts = str(full_name or "").strip().split()
    return parts[0] if parts else "there"


def _email_alias(record: dict[str, Any]) -> str | None:
    for alias in record.get("aliases") or []:
        text = str(alias).strip()
        if "@" in text and " " not in text:
            return text
    return None


def owner_signature(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """The owner's name and reply address, from the personal knowledge base.

    Read rather than configured, because these are facts about the owner and the `fact`
    table is where those live (CLAUDE.md §Owner memory). Absent facts degrade to an
    unsigned draft the owner finishes by hand — never to an invented name.
    """
    name: str | None = None
    email: str | None = None
    for row in conn.execute(
        "SELECT key, value FROM fact WHERE user_id = ? AND status = 'active'"
        " AND subject = 'identity' AND key IN ('name', 'emails')",
        (USER_ID,),
    ):
        if str(row["key"]) == "name":
            name = str(row["value"]).strip() or None
        else:
            # `identity/emails` is a human-written list: "a@b.com (personal) · c@d.edu
            # (ASU)". First entry wins, parenthetical label dropped.
            head = str(row["value"]).split("·")[0]
            candidate = head.split("(")[0].strip()
            email = candidate or None
    return name, email


def _touch_for(
    conn: sqlite3.Connection, settings: Settings, day: date, entity_id: int
) -> touch.Touch | None:
    for candidate in touch.cold(conn, settings, day):
        if candidate.entity_id == entity_id:
            return candidate
    return None


def _paragraph(text: str, slots: dict[str, str]) -> str | None:
    """Fill one paragraph; a paragraph that is nothing but an empty slot disappears.

    Two behaviours, and the distinction is the whole reason a person with no evidence
    still gets a readable email. An *inline* slot renders empty and the sentence around
    it survives — "since we last spoke{gap} and you came to mind" reads correctly with
    no gap to state. A paragraph that is *only* a slot, like the note line, has nothing
    left to say when the slot is empty, so it is dropped rather than left as a blank
    line in the middle of the message.
    """
    filled = text
    for name, value in slots.items():
        filled = filled.replace("{" + name + "}", value)
    bare = text.strip()
    only_a_slot = bare.startswith("{") and bare.endswith("}") and bare.count("{") == 1
    if only_a_slot and not filled.strip():
        return None
    # Slots collapse to nothing mid-sentence; tidy the seams they leave without
    # touching the newline inside the sign-off.
    lines = [" ".join(line.split()) for line in filled.split("\n")]
    tidy = "\n".join(lines).strip()
    return tidy or None


def draft(
    conn: sqlite3.Connection,
    settings: Settings,
    entity_id: int,
    *,
    note: str,
    template: str = "thanks",
    where: str | None = None,
    subject: str | None = None,
    day: date | None = None,
) -> Draft:
    """Render one draft. Reads only — nothing here writes a row.

    `note` is required and is the whole point: the specific thing that happened between
    these two people, in the owner's words. A template with an empty note is a form
    letter, and a form letter sent to someone you met in person is worse than silence.
    """
    note = (note or "").strip()
    if not note:
        raise ReachoutError(
            "a draft needs --note: the specific thing you want to say to them, "
            "in your words. Without it this is a form letter."
        )
    if template not in TEMPLATES:
        known = ", ".join(sorted(TEMPLATES))
        raise ReachoutError(f"unknown template {template!r}; known templates: {known}")

    record = profiles.profile(conn, entity_id)
    if record is None:
        raise ReachoutError(f"no person with id {entity_id}")

    spec = TEMPLATES[template]
    owner_name, owner_email = owner_signature(conn)
    to_name = str(record["canonical_name"])
    first = _first_name(to_name)
    warmth = _touch_for(conn, settings, day or date.today(), entity_id)

    evidence: list[str] = [
        f"recipient: entity #{entity_id} '{to_name}'"
        f" — SELECT * FROM entity WHERE id = {entity_id}",
        f"template: {spec.name} — backglass/people/reachout.py TEMPLATES",
        "note: typed by the owner for this draft, not extracted",
    ]

    gap = ""
    if warmth is not None and warmth.days_since is not None:
        gap = f" — about {warmth.days_since} days"
        source = warmth.source_row or {}
        evidence.append(
            f"gap: {warmth.days_since} days since source_item"
            f" #{source.get('source_item_id')} ({source.get('source')},"
            f" {str(source.get('source_occurred_at') or '')[:10]})"
        )
    elif warmth is not None:
        evidence.append(
            "gap: omitted — no interaction on record for this person, so no honest"
            " number to state"
        )
    else:
        evidence.append(
            "gap: omitted — profile is not curated (no role, org or tag), so"
            " people_cold.sql does not return it"
        )

    org = str(record.get("org") or "").strip()
    # Only claim a source for something the reader can actually find in the draft.
    # Two of the three templates never render {at_org}, and an evidence line naming a
    # row that produced no words is the shape of a provenance list nobody checks twice
    # (CLAUDE.md rule 1). The org still reaches the draft through the entity row when a
    # template asks for it; it is silent otherwise.
    if org and "{at_org}" in spec.subject + "".join(spec.paragraphs):
        evidence.append(f"organisation: entity.org = '{org}'")
    if owner_name:
        evidence.append("sign-off: fact identity/name")
    if owner_email:
        evidence.append("reply address: fact identity/emails, first entry")

    to_email = _email_alias(record)
    if to_email:
        evidence.append(f"address: entity.aliases_json of #{entity_id}")
    else:
        evidence.append(
            "address: none on the profile — the draft is text to paste, and an alias"
            " added to the profile turns it into a mailto link"
        )

    where_text = (where or "").strip()
    slots = {
        "first": first,
        "note": note,
        "where": f" {where_text}" if where_text else "",
        "gap": gap,
        "at_org": f" at {org}" if org else "",
        "owner": owner_name or "",
        "owner_first": _first_name(owner_name) if owner_name else "",
        "owner_email_line": f"\n{owner_email}" if owner_email else "",
    }

    paragraphs = [
        text for text in (_paragraph(p, slots) for p in spec.paragraphs) if text
    ]
    body = "\n\n".join(paragraphs)
    line = (subject or "").strip() or spec.subject
    for name, value in slots.items():
        line = line.replace("{" + name + "}", value)
    line = " ".join(line.split())

    return Draft(
        entity_id=entity_id,
        to_name=to_name,
        to_email=to_email,
        template=spec.name,
        subject=line,
        body=body,
        evidence=tuple(evidence),
    )


def resolve(conn: sqlite3.Connection, term: str) -> int:
    """A person id from an id or a name. Ambiguity is an error, never a guess.

    Two people whose names both match is the case that has to fail loudly: this feeds a
    command that produces something the owner sends, and a draft addressed to the wrong
    person is worse than no draft. Same instinct as contacts.py rule 3.
    """
    term = (term or "").strip()
    if not term:
        raise ReachoutError("name or id required")
    if term.isdigit():
        record = profiles.profile(conn, int(term))
        if record is None:
            raise ReachoutError(f"no person with id {term}")
        return int(record["id"])
    hits = [r for r in profiles.search(conn, q=term) if str(r.get("kind")) == "person"]
    if not hits:
        raise ReachoutError(
            f"no person matching {term!r}. `backglass people` lists who exists, and the"
            " People page creates someone the ledger has never seen."
        )
    exact = [
        r for r in hits if str(r["canonical_name"]).strip().lower() == term.lower()
    ]
    if len(exact) == 1:
        return int(exact[0]["id"])
    if len(hits) > 1:
        names = ", ".join(f"#{r['id']} {r['canonical_name']}" for r in hits[:6])
        raise ReachoutError(f"{term!r} matches {len(hits)} people: {names}")
    return int(hits[0]["id"])


def candidates(
    conn: sqlite3.Connection, settings: Settings, day: date
) -> list[touch.Touch]:
    """Who is worth a draft today, coldest first.

    Deliberately `touch.cold` rather than `needing_follow_up`: the latter requires a
    source row, which is the one thing an in-person connection does not have. Someone
    curated and never heard from since is the top of this list, not absent from it.
    """
    ranked = touch.cold(conn, settings, day)
    return [t for t in ranked if t.level in ("new", "warn", "cold")]


def to_json(record: Draft) -> str:
    return json.dumps(
        {
            "entity_id": record.entity_id,
            "to_name": record.to_name,
            "to_email": record.to_email,
            "template": record.template,
            "subject": record.subject,
            "body": record.body,
            "evidence": list(record.evidence),
        },
        indent=2,
    )


__all__ = [
    "Draft",
    "ReachoutError",
    "TEMPLATES",
    "Template",
    "candidates",
    "draft",
    "owner_signature",
    "resolve",
    "to_json",
]
