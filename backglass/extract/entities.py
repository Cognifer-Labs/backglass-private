"""Entity resolution and the fuzzy comparison used for commitment dedup.

docs/03 §entity: "Resolution merges 'Dave', 'David R.', and drodriguez@… into one entity.
Get this wrong and the 'awaiting others' view fragments into duplicates and stops being
useful."
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from email.utils import parseaddr

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")

#: Words that carry no identifying signal in a commitment description. Dropped before
#: comparison so "the revised migration plan" and "revised migration plan" are one thing.
_STOPWORDS = frozenset(
    {"a", "an", "the", "to", "for", "of", "on", "in", "and", "with", "by", "at", "from"}
)


def parse_counterparty(raw: str) -> tuple[str, str | None]:
    """Split `Dana Whitfield <dwhitfield@example.gov>` into a name and an address.

    The address is the reliable key and becomes an alias. The display name is a fallback,
    because the same person appears as "Dana", "Dana Whitfield" and "D. Whitfield" across
    a single thread.
    """
    parsed_name, parsed_email = parseaddr(raw.strip())
    email: str | None = parsed_email.strip().lower() or None
    if email and "@" not in email:
        email = None
    name = parsed_name
    if not name.strip():
        name = raw.strip() if not email else email.split("@")[0].replace(".", " ").title()
    return name.strip().strip('"'), email


def normalize(text: str) -> frozenset[str]:
    """A comparable token set: lowercased, punctuation removed, stopwords dropped."""
    cleaned = _PUNCT.sub(" ", text.lower())
    tokens = [token for token in _WS.split(cleaned) if token and token not in _STOPWORDS]
    return frozenset(tokens)


def similar(left: str, right: str) -> float:
    """Normalized token-set ratio, 0.0 to 1.0.

    extract-commitments.md §Post-processing step 5 says "fuzzy match" without naming an
    algorithm. Ruling (tasks/todo.md §Deviations #6): token-set ratio over stdlib difflib.
    No new dependency, the threshold is one config value, and word order stops mattering —
    "send Dana the scope" and "scope sent to Dana" collapse, which is exactly the thread
    restatement case the step exists to catch.
    """
    a, b = normalize(left), normalize(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    overlap = a & b
    # Jaccard alone is too harsh on one-word differences at these lengths; the sequence
    # ratio over sorted tokens recovers near-misses like "plan" vs "plans".
    jaccard = len(overlap) / len(a | b)
    sequence = SequenceMatcher(None, " ".join(sorted(a)), " ".join(sorted(b))).ratio()
    return max(jaccard, sequence)
