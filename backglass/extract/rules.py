"""Tier 0: the rule layer. docs/02 §Tier 1 triage — "Rules first, and rules handle most of it."

Every item this layer drops is a model call not made. docs/02 expects tiers 0 and 1
together to eliminate 90–95% of volume; if the measured kill rate falls below 85%, the
rules have drifted and the cost model is about to break. That number is computed by
db/queries/triage_kill_rate.sql and belongs in the Sources panel.

Each rule records *which* rule fired, in `source_item.triage_reason`. triage.md: reading
a week of reasons is how you find the rule that should have caught something earlier.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from email.utils import parseaddr

#: Bulk-mail headers. Their presence is close to a definition of "not personal mail".
_BULK_HEADERS = (
    "list-unsubscribe",
    "list-id",
    "list-post",
    "x-campaign-id",
    "x-mailer-campaign",
    "feedback-id",
)
_BULK_PRECEDENCE = {"bulk", "list", "junk", "auto_reply"}

#: Local-parts that never carry a commitment. Kept narrow on purpose: `support@` and
#: `billing@` are excluded from this list because a human on a support alias absolutely
#: does make commitments, and triage.md is explicit that a false negative loses a
#: commitment permanently while a false positive costs one call.
_NOREPLY = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|mailer[-_.]?daemon|postmaster|"
    r"bounce[sd]?|notifications?|automated|auto[-_.]?confirm)",
    re.IGNORECASE,
)

_CALENDAR_MIME = "text/calendar"

#: Every reason string this layer can produce starts with one of these. A stored drop
#: whose reason matches none of them was therefore a *model* drop — the discrimination
#: learned-noise mining (extract/noise.py) rests on. Kept here, next to the strings
#: themselves, so a new rule reason and its prefix are a same-file edit.
RULE_REASON_PREFIXES = (
    "bulk header:",
    "precedence:",
    "auto-submitted",
    "no-reply sender:",
    "known-noise",
    "calendar invite",
    "structured source",
    "template:",
)


@dataclass(frozen=True)
class RuleVerdict:
    #: 'drop' or 'unclassified'. Rules never return 'keep' — a rule cannot establish that
    #: something is worth extracting, only that it is not. Anything they cannot classify
    #: goes to the tier-1 model.
    verdict: str
    reason: str | None = None

    @property
    def dropped(self) -> bool:
        return self.verdict == "drop"


UNCLASSIFIED = RuleVerdict(verdict="unclassified")


#: Any letter or digit, in any script. `\w` would match underscores and `str.isalpha`
#: would need a loop; this is the whole test for "is there anything here to read".
_HAS_CONTENT = re.compile(r"[^\W_]", re.UNICODE)


def classify(
    *,
    headers: dict[str, str],
    author: str | None,
    raw_json: str | None = None,
    body_text: str | None = None,
    noise_senders: frozenset[str] = frozenset(),
    source: str = "",
    structured_sources: frozenset[str] = frozenset(),
) -> RuleVerdict:
    # Cheaper than every rule below it, because it needs no headers at all: an item with
    # no letters or digits cannot carry a commitment, a plan, or a date. On the owner's
    # 3,687 real messages this is 401 items — 10.9% — that were each costing a model call
    # to be told an emoji has no substance.
    #
    # Deliberately *only* "no alphanumeric content", not "short". Measuring a candidate
    # pleasantry list against the same 3,687 messages killed 41 the model had kept: "Ok",
    # "Yes", "bet". Those are confirmations, and a bare "Yes" answering "dinner Friday?"
    # is exactly the sighting the engagement extractor exists to catch. Length is not a
    # proxy for meaning in a conversation, and the cheap rule that looks obviously safe
    # here is the one that silently eats agreements.
    if body_text is not None and not _HAS_CONTENT.search(body_text):
        return RuleVerdict("drop", "no letters or digits")

    # Cheapest rule first: items from a structured source (anki, avorio) are consumed
    # deterministically by their own readers; a model would find nothing to extract.
    if source:
        for entry in structured_sources:
            if source == entry or source.startswith(entry + ":"):
                return RuleVerdict(
                    "drop", f"structured source: {entry}; consumed deterministically"
                )

    lowered = {key.lower(): value for key, value in headers.items()}

    for header in _BULK_HEADERS:
        if header in lowered:
            return RuleVerdict("drop", f"bulk header: {header}")

    precedence = lowered.get("precedence", "").strip().lower()
    if precedence in _BULK_PRECEDENCE:
        return RuleVerdict("drop", f"precedence: {precedence}")

    if lowered.get("auto-submitted", "").strip().lower() not in ("", "no"):
        return RuleVerdict("drop", "auto-submitted")

    address = _address(author or lowered.get("from", ""))
    if address:
        local, _, domain = address.partition("@")
        if _NOREPLY.match(local):
            return RuleVerdict("drop", f"no-reply sender: {local}@")
        if address in noise_senders:
            return RuleVerdict("drop", f"known-noise sender: {address}")
        # Subdomain-aware, the same way boundary.py reads a domain entry, so that one
        # `example.com` entry covers `mail.example.com` without a second line of config.
        for denied in noise_senders:
            if domain and (domain == denied or domain.endswith("." + denied)):
                return RuleVerdict("drop", f"known-noise domain: {denied}")

    # docs/02: calendar invites are "handled by the calendar connector, not extraction".
    if _is_calendar_invite(lowered, raw_json):
        return RuleVerdict("drop", "calendar invite; owned by the calendar connector")

    return UNCLASSIFIED


def _address(raw: str) -> str:
    return parseaddr(raw)[1].lower().strip()


def _is_calendar_invite(lowered: dict[str, str], raw_json: str | None) -> bool:
    content_type = lowered.get("content-type", "").lower()
    if _CALENDAR_MIME in content_type or "method=" in content_type:
        return True
    if not raw_json:
        return False
    try:
        parsed = json.loads(raw_json)
    except (ValueError, TypeError):
        return False
    if not isinstance(parsed, dict):
        return False
    headers = parsed.get("headers")
    if isinstance(headers, dict):
        joined = " ".join(
            str(v).lower() for k, v in headers.items() if k.lower() == "content-type"
        )
        if _CALENDAR_MIME in joined:
            return True
    return False
