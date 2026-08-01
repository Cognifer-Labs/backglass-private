"""Learned noise: senders the tier-1 model keeps dropping become free tier-0 drops.

rules.py's docstring says reading a week of triage reasons is how you find the rule
that should have caught something earlier. This module is that reading, automated.

The promotion bar is absolute, not statistical: a candidate needs `min_evidence`
model drops AND zero keeps across all history AND no commitment ever extracted from
its mail. One keep, ever, permanently disqualifies — triage.md: a false negative
loses a commitment permanently, a false positive costs one call.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from backglass.config import Settings
from backglass.db import now_iso, query
from backglass.extract.rules import RULE_REASON_PREFIXES, _address
from backglass.ledger import USER_ID


@dataclass(frozen=True)
class Candidate:
    kind: str  # 'address' | 'domain'
    value: str
    evidence_count: int
    first_seen: str | None
    last_seen: str | None
    sample_reason: str | None


@dataclass
class _Tally:
    model_drops: int = 0
    keeps: int = 0
    first: str | None = None
    last: str | None = None
    reason: str | None = field(default=None)

    def see(self, occurred: str) -> None:
        self.first = occurred if self.first is None else min(self.first, occurred)
        self.last = occurred if self.last is None else max(self.last, occurred)


def _is_rule_reason(reason: str | None) -> bool:
    if not reason:
        return False
    return any(reason.startswith(prefix) for prefix in RULE_REASON_PREFIXES)


def enabled_entries(conn: sqlite3.Connection) -> frozenset[str]:
    """Promoted values, ready to be unioned with settings.noise_senders.

    rules.classify already does address-equality and subdomain-aware domain matching
    over that set, so consumption needs no new rule code at all.
    """
    rows = conn.execute(
        "SELECT value FROM learned_noise WHERE user_id = ? AND enabled = 1", (USER_ID,)
    )
    return frozenset(str(r["value"]) for r in rows)


def _covered(value: str, settings: Settings, already: frozenset[str]) -> bool:
    if value in already or value in settings.noise_senders:
        return True
    domain = value.partition("@")[2] or value
    return any(
        domain == entry or domain.endswith("." + entry)
        for entry in set(settings.noise_senders) | already
        if "@" not in entry
    )


def candidates(
    conn: sqlite3.Connection, settings: Settings, min_evidence: int = 5
) -> list[Candidate]:
    """Mine triage history for promotable senders. Read-only; promotion is separate."""
    stats: dict[str, _Tally] = {}
    for row in conn.execute(query("noise_evidence"), {"user_id": USER_ID}):
        address = _address(str(row["author"]))
        if not address:
            continue
        tally = stats.setdefault(address, _Tally())
        tally.see(str(row["occurred_at"]))
        if row["triage_verdict"] == "keep":
            tally.keeps += 1
        elif row["triage_verdict"] == "drop" and not _is_rule_reason(row["triage_reason"]):
            tally.model_drops += 1
            tally.reason = row["triage_reason"]

    already = enabled_entries(conn)
    out: list[Candidate] = []
    for address, tally in sorted(stats.items()):
        if tally.model_drops < min_evidence or tally.keeps > 0:
            continue
        if settings.owns(address) or _covered(address, settings, already):
            continue
        # Belt and braces: keeps == 0 already implies nothing was extracted, but the
        # commitment ledger is the ground truth this rule protects, so ask it directly.
        extracted = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM commitment c JOIN source_item si"
            " ON si.id = c.source_item_id"
            " WHERE si.user_id = ? AND lower(si.author) LIKE ?) AS x",
            (USER_ID, f"%{address}%"),
        ).fetchone()
        if extracted["x"]:
            continue
        out.append(
            Candidate(
                kind="address",
                value=address,
                evidence_count=tally.model_drops,
                first_seen=tally.first,
                last_seen=tally.last,
                sample_reason=tally.reason,
            )
        )

    # Domain candidates are reported, never auto-promoted: promoting a whole domain is
    # always an explicit CLI act because the blast radius is every future sender on it.
    by_domain: dict[str, list[Candidate]] = {}
    for c in out:
        by_domain.setdefault(c.value.partition("@")[2], []).append(c)
    for domain, members in sorted(by_domain.items()):
        if len(members) >= 3 and not _covered(domain, settings, already):
            out.append(
                Candidate(
                    kind="domain",
                    value=domain,
                    evidence_count=sum(m.evidence_count for m in members),
                    first_seen=min(m.first_seen or "" for m in members) or None,
                    last_seen=max(m.last_seen or "" for m in members) or None,
                    sample_reason=f"{len(members)} distinct qualifying addresses",
                )
            )
    return out


def promote(
    conn: sqlite3.Connection,
    values: list[Candidate],
    *,
    by: str,
    now: str | None = None,
) -> int:
    """Insert promotions; already-promoted values are skipped. Returns rows written."""
    written = 0
    stamp = now or now_iso()
    for c in values:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO learned_noise"
            " (user_id, kind, value, evidence_count, first_seen, last_seen,"
            "  promoted_at, promoted_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (USER_ID, c.kind, c.value, c.evidence_count, c.first_seen, c.last_seen,
             stamp, by),
        )
        written += cursor.rowcount
    return written
