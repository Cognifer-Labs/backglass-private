"""The macOS address book, via the OS automation bridge. Phase 13.

Same bridge as Notes, Reminders and Calendar — `Application('Contacts')` over the JXA
runner in `apple_notes` — so this needs no OAuth client, no consent flow and no Full
Disk Access. The permission is the Automation one the project already holds, which is
the whole reason this source exists at all: the answer to "who is +14802411748" was
sitting behind a door that was already open (tasks/lessons.md, 2026-08-02).

**Not a `Connector`.** It has no cursor, yields no `SourceItem`, and never touches
`source_item` — a contact is reference data, not an event (see backglass/contacts.py for
the argument). What it shares with the connectors is the two things the run needs from
any source: an injected `runner`, so no test reaches osascript, and a `health()` that
turns a denied Automation prompt into a product state rather than a traceback.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from backglass.connectors.apple_notes import run_osascript
from backglass.connectors.base import Health
from backglass.connectors.boundary import Boundary
from backglass.contacts import Contact

SOURCE = "apple-contacts"

#: One JSON array of {id, name, org, isCompany, phones, emails}. Deliberately narrow:
#: addresses, birthdays and notes are none of the ledger's business, and a card the
#: owner never asked to be indexed should leave as little behind as it can.
_SCRIPT = """
const app = Application('Contacts');
const out = [];
const people = app.people();
for (let i = 0; i < people.length; i++) {
  const p = people[i];
  out.push({
    id: p.id(),
    name: p.name(),
    org: p.organization() || '',
    isCompany: p.company() === true,
    phones: p.phones().map(function (x) { return x.value(); }),
    emails: p.emails().map(function (x) { return x.value(); }),
  });
}
JSON.stringify(out);
"""


@dataclass(kw_only=True)
class ContactsSource:
    boundary: Boundary
    #: Injected so tests never touch osascript, mirroring every other Apple source.
    runner: Callable[[str], str] = run_osascript

    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return SOURCE

    def health(self) -> Health:
        try:
            self.runner("Application('Contacts').name();")
        except Exception as exc:  # noqa: BLE001 — every failure is a product state here
            return Health(
                name=self.name,
                ok=False,
                detail=(
                    f"Contacts automation unavailable: {exc}. Grant Automation access "
                    "in System Settings → Privacy & Security → Automation."
                ),
            )
        return Health(name=self.name, ok=True)

    def read(self) -> list[Contact]:
        """Every card, boundary-checked before it becomes a `Contact`.

        D1: the denylist runs in the source, before anything is persisted. A contact is
        the client's name and address in one row, which is precisely what docs/08 says
        never enters the store — so an excluded card is counted and dropped here rather
        than filtered later.
        """
        cards = json.loads(self.runner(_SCRIPT) or "[]")
        out: list[Contact] = []
        for card in cards:
            name = str(card.get("name") or "").strip()
            if not name:
                # An address book holds cards that are only a company or only a number.
                # Without a name there is nothing to display, which is the entire point.
                continue
            emails = [str(e) for e in card.get("emails") or []]
            verdict = self.boundary.check(emails)
            if not verdict.allowed:
                self.excluded += 1
                rule = verdict.matched_rule or "boundary"
                self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + 1
                continue
            phones = [str(p) for p in card.get("phones") or []]
            out.append(
                Contact(
                    uid=str(card.get("id") or name),
                    name=name,
                    identifiers=tuple(phones + emails),
                    is_org=bool(card.get("isCompany")),
                )
            )
        return out
