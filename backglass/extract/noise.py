"""Learned noise: senders the tier-1 model keeps dropping become free tier-0 drops.

rules.py's docstring says reading a week of triage reasons is how you find the rule
that should have caught something earlier. This module is that reading, automated.

Two observations count as evidence, and the difference between them is the whole of
this module's 2026-08-06 revision:

1. **The model dropped it.** The original class, unchanged.
2. **The model kept it and the expensive pass found nothing** — on mail that carries a
   machine-readable broadcast marker. This was invisible before, and it was the
   dominant one. Counted through this module's own query against the owner's ledger:
   of 614 kept items, 611 reached a completed extraction and **539 of those produced no
   record at all** — 417 of the 539 on broadcast-marked mail — while `learned_noise`
   held zero rows. The loop could not learn from its most expensive mistake, because
   the disqualifier was "was it ever kept" — and college marketing mail is kept exactly
   because it is written to look like a deadline.

The disqualifier is therefore no longer "one keep, ever" but **"anything ever came of
it, or anything is still open"**: a commitment, an engagement, a fact, a citation or a
goal checkpoint disqualifies forever, and so does a keep the expensive pass has not yet
settled — pending or parked. An unanswered keep is an open question, not a resolved
nothing, so the bar moves only for keeps that have actually been answered.

The promotion bar remains absolute rather than statistical: `min_evidence` observations
AND zero productive items across all history AND no commitment ever extracted from its
mail, asked twice by two paths. triage.md's asymmetry is untouched — a false negative
loses a commitment permanently, a false positive costs one call — which is why the
barren class requires the broadcast marker (personal mail never carries one) and why
promotion stays an explicit act with the owner reading the evidence first.
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
    #: The two evidence classes, kept apart on purpose. A model drop and a keep the
    #: expensive pass then proved empty are different observations, and the owner
    #: deciding whether to promote should see which one they are looking at.
    model_drops: int = 0
    barren_bulk_keeps: int = 0
    #: A kept subject line from the barren evidence, so review is informed by what the
    #: sender actually writes rather than by a domain name. `e.salliemae.com` looks like
    #: junk in a list and carries a real loan deadline once a year.
    sample_title: str | None = None


@dataclass
class _Tally:
    model_drops: int = 0
    #: Kept, extracted to completion, produced nothing, and machine-marked as broadcast.
    #: All four conditions: see `candidates` for why none of them can be dropped.
    barren_bulk_keeps: int = 0
    #: Anything this sender ever produced. The first disqualifier.
    productive: int = 0
    #: Kept and not yet resolved: the expensive pass has not run, or ran and parked.
    #: The second disqualifier, and the reason this revision does not loosen the bar —
    #: an unanswered keep is an open question, not a resolved nothing, and the next
    #: extract pass may well turn it into a commitment.
    unresolved_keeps: int = 0
    first: str | None = None
    last: str | None = None
    reason: str | None = field(default=None)
    title: str | None = field(default=None)

    @property
    def evidence(self) -> int:
        return self.model_drops + self.barren_bulk_keeps

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
        if row["produced"]:
            # Ground truth, and the only disqualifier. Asked before anything else so a
            # sender that has produced once can never accumulate evidence against
            # itself on the mail that produced nothing.
            tally.productive += 1
        elif row["triage_verdict"] == "keep":
            # A keep the expensive pass then proved empty. Four conditions, none
            # removable:
            #   produced == 0  the pass found nothing (checked above)
            #   extracted      the pass RAN — an unextracted keep is pending, and a
            #                  parked one failed, which is a bug's evidence, not noise's
            #   bulk           only broadcast mail qualifies, so this class can never
            #                  reach a human correspondent whose first mails happen to
            #                  be informational
            # This is not tuning away triage.md's "when in doubt, keep". The model's
            # doubt is honoured exactly as before; what changed is that its doubt,
            # once resolved to nothing by the pass that costs real money, finally
            # counts as the observation it always was.
            if not row["extracted"]:
                # Pending, or parked by a failure. Either way nobody has answered the
                # question this keep asks, so it disqualifies exactly as any keep did
                # before — the bar moves only for keeps the pass has actually settled.
                tally.unresolved_keeps += 1
            elif row["bulk"]:
                tally.barren_bulk_keeps += 1
                tally.title = tally.title or str(row["title"] or "") or None
        elif row["triage_verdict"] == "drop" and not _is_rule_reason(row["triage_reason"]):
            tally.model_drops += 1
            tally.reason = row["triage_reason"]

    already = enabled_entries(conn)
    out: list[Candidate] = []
    for address, tally in sorted(stats.items()):
        if tally.evidence < min_evidence:
            continue
        if tally.productive > 0 or tally.unresolved_keeps > 0:
            continue
        if settings.owns(address) or _covered(address, settings, already):
            continue
        # Belt and braces. `productive == 0` already covers this through the query's
        # own EXISTS, but the commitment ledger is the ground truth this rule protects
        # and it is asked directly, by a second path, on purpose. It also catches the
        # case the join cannot: a commitment whose source item was recorded under a
        # differently-formatted copy of the same address.
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
                evidence_count=tally.evidence,
                first_seen=tally.first,
                last_seen=tally.last,
                sample_reason=tally.reason,
                model_drops=tally.model_drops,
                barren_bulk_keeps=tally.barren_bulk_keeps,
                sample_title=tally.title,
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
                    model_drops=sum(m.model_drops for m in members),
                    barren_bulk_keeps=sum(m.barren_bulk_keeps for m in members),
                    sample_title=next(
                        (m.sample_title for m in members if m.sample_title), None
                    ),
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
