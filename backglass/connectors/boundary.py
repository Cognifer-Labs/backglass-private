"""The data boundary from docs/08-privacy-and-data-boundary.md.

This module lives in the connector package deliberately (docs/10 §Layout): "Putting it
in a filter or a middleware invites someone to bypass it; being the thing a connector
calls before it yields makes bypassing it a visible edit."

Requirements implemented here, by ID:

  D1  the check runs in the connector, before persistence
  D2  the denylist is configuration, versioned in the repo
  D3  domain and explicit address, case-insensitive, subdomains included
  D4  a match produces no source_item row and no model call
  D5  excluded counts are recorded per run
  D6  adding a domain purges previously stored matching items, with a report

D7 (a message with a denylisted address in ANY recipient field produces zero rows) is a
test, in tests/test_boundary.py, and it does not get skipped.

The owner has selected BOUNDARY_MODE=full_scope, under which `check` always allows.
The machinery is still here and still tested, because D6 exists precisely for the day
the denylist turns out to have been wrong, and switching modes should be a config change
rather than a build.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from email.utils import getaddresses

from backglass.config import Settings

#: Header fields the boundary is evaluated over. docs/08: "Any message with a denylisted
#: address in `From`, `To`, `Cc`, or `Bcc` is dropped entirely."
RECIPIENT_HEADERS = ("from", "to", "cc", "bcc", "reply-to")


@dataclass(frozen=True)
class BoundaryVerdict:
    allowed: bool
    #: The denylist entry that matched, for the run report. Never the address itself —
    #: docs/08 says excluded content is not retained in any form, and the address is
    #: excluded content. The rule that fired is configuration, so it is safe to log.
    matched_rule: str | None = None


@dataclass
class PurgeReport:
    """D6. What a denylist addition removed."""

    source_items: int = 0
    commitments: int = 0
    matched_rules: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.source_items + self.commitments


def addresses_in(headers: dict[str, str]) -> list[str]:
    """Every address appearing in any recipient header, lowercased.

    Uses `email.utils.getaddresses` rather than splitting on commas, because a display
    name may legitimately contain a comma: `"Whitfield, Dana" <d@example.gov>`.
    """
    values = [headers[key] for key in headers if key.lower() in RECIPIENT_HEADERS]
    return [addr.strip().lower() for _, addr in getaddresses(values) if addr.strip()]


class Boundary:
    """Evaluates the denylist. Constructed once per run and passed into each connector."""

    def __init__(
        self,
        mode: str,
        deny_domains: Sequence[str] = (),
        deny_addresses: Sequence[str] = (),
    ) -> None:
        self.mode = mode
        self.deny_domains = tuple(
            d.strip().lower().lstrip("@") for d in deny_domains if d.strip()
        )
        self.deny_addresses = tuple(a.strip().lower() for a in deny_addresses if a.strip())

    @classmethod
    def from_settings(cls, settings: Settings) -> Boundary:
        return cls(
            mode=settings.boundary_mode,
            deny_domains=settings.boundary_deny_domains,
            deny_addresses=settings.boundary_deny_addresses,
        )

    @property
    def enforcing(self) -> bool:
        return self.mode == "exclude" and bool(self.deny_domains or self.deny_addresses)

    def match(self, address: str) -> str | None:
        """The denylist entry this address trips, or None.

        D3: case-insensitive, and a domain entry covers its subdomains — `example.gov`
        matches `wic.example.gov` but not `notexample.gov`.
        """
        address = address.strip().lower()
        if not address:
            return None
        if address in self.deny_addresses:
            return address
        _, _, domain = address.rpartition("@")
        if not domain:
            return None
        for denied in self.deny_domains:
            if domain == denied or domain.endswith("." + denied):
                return denied
        return None

    def check(self, addresses: Iterable[str]) -> BoundaryVerdict:
        """D4. One denylisted address anywhere drops the whole message."""
        if self.mode == "full_scope":
            return BoundaryVerdict(allowed=True)
        for address in addresses:
            matched = self.match(address)
            if matched is not None:
                return BoundaryVerdict(allowed=False, matched_rule=matched)
        return BoundaryVerdict(allowed=True)

    def check_headers(self, headers: dict[str, str]) -> BoundaryVerdict:
        return self.check(addresses_in(headers))


def purge(
    conn: sqlite3.Connection, boundary: Boundary, *, dry_run: bool = False
) -> PurgeReport:
    """D6. Remove already-stored items that a newly added denylist entry now matches.

    The denylist is incomplete on day one; docs/08 says there must be a clean way to fix
    that discovery. Deletion is genuine deletion, not a tombstone, because docs/08 also
    says excluded content is not retained in any form "including hashes computed over it".
    That is the one place the retain-forever rule in docs/03 does not apply.
    """
    report = PurgeReport()
    if not boundary.enforcing:
        return report

    doomed: list[int] = []
    for row in conn.execute("SELECT id, author, raw_json FROM source_item"):
        headers = _headers_from_raw(row["raw_json"])
        candidates = addresses_in(headers)
        if row["author"]:
            candidates.extend(a for _, a in getaddresses([str(row["author"])]) if a)
        verdict = boundary.check(candidates)
        if not verdict.allowed:
            doomed.append(int(row["id"]))
            rule = verdict.matched_rule or "?"
            report.matched_rules[rule] = report.matched_rules.get(rule, 0) + 1

    if not doomed:
        return report

    placeholders = ",".join("?" for _ in doomed)
    report.commitments = int(
        conn.execute(
            f"SELECT COUNT(*) AS n FROM commitment WHERE source_item_id IN ({placeholders})",
            doomed,
        ).fetchone()["n"]
    )
    report.source_items = len(doomed)

    if dry_run:
        return report

    conn.execute("BEGIN")
    try:
        # Commitments first: commitment.source_item_id is NOT NULL and has no cascade,
        # so the foreign key would reject the parent delete otherwise.
        conn.execute(f"DELETE FROM commitment WHERE source_item_id IN ({placeholders})", doomed)
        conn.execute(f"DELETE FROM source_item WHERE id IN ({placeholders})", doomed)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return report


def _headers_from_raw(raw_json: object) -> dict[str, str]:
    if not raw_json:
        return {}
    try:
        parsed = json.loads(str(raw_json))
    except (ValueError, TypeError):
        return {}
    headers = parsed.get("headers") if isinstance(parsed, dict) else None
    if not isinstance(headers, dict):
        return {}
    return {str(k): str(v) for k, v in headers.items()}
