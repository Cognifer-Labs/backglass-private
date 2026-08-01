"""Template hashing: catch the weekly statement's fiftieth sibling for free.

A templated mail differs from its siblings only in the variable parts — amounts,
dates, order numbers, links. Strip those and hash what remains, scoped to the sender
domain, and recurring notifications collapse onto one hash. The rule layer then drops
a new sibling only after `template_drop_after` prior ones were all dropped and none
was ever kept (sync._rule_pass) — evidence is paid for once, then reused for free.

Strip order matters: URLs first (they contain digits), then dates, then bare digits.
Deterministic, no model, inspectable via `backglass noise templates`.
"""

from __future__ import annotations

import hashlib
import re

from backglass.extract.rules import _address

_URL = re.compile(r"https?://\S+")
_DATE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,? ?\d{1,4}\b",
    re.IGNORECASE,
)
# Whole numeric tokens including grouping/decimal separators, so "$1,234.56" and
# "$88.20" collapse to the same shape.
_DIGITS = re.compile(r"\d+(?:[.,]\d+)*")

#: Only this much of the body feeds the skeleton. Templates announce themselves in the
#: first screenful; hashing 60k of boilerplate would just slow ingest.
BODY_WINDOW = 2000
SKELETON_LIMIT = 1000


def _skeleton(text: str) -> str:
    text = _URL.sub("<url>", text)
    text = _DATE.sub("<date>", text)
    text = _DIGITS.sub("<n>", text)
    return " ".join(text.lower().split())[:SKELETON_LIMIT]


def template_hash(
    *, author: str | None, title: str | None, body_text: str | None
) -> str | None:
    """The item's template identity, or None for anything that is not mail-shaped.

    Anki tallies, notes, and files have no sender domain and are not templated mail;
    they return None and never participate in template matching.
    """
    address = _address(author or "")
    domain = address.partition("@")[2]
    if not domain:
        return None
    parts = [
        domain,
        _skeleton(title or ""),
        _skeleton((body_text or "")[:BODY_WINDOW]),
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
