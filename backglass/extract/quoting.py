"""Quoted history and signature stripping for plain-text email.

Vendored, not depended on. The two projects that solved this are both abandonware:

  - **mailgun/talon** (Apache-2.0) — https://github.com/mailgun/talon — last real commit
    Feb 2022. Its plain-text patterns in `talon/quotations.py` (the "On DATE, NAME
    wrote:" splitter with its six-line window, `-----Original Message-----`, the
    `From:`/`Sent:` Outlook block, the `>`-prefix line classifier) and the brute-force
    sign-off patterns in `talon/signature/bruteforce.py` are reproduced here in spirit.
  - **zapier/email-reply-parser** (MIT) — https://github.com/zapier/email-reply-parser —
    last commit 2020. Its `QUOTE_HDR_REGEX` and `SIG_REGEX` (the `--`/`__` delimiters and
    the "Sent from my iPhone" family) are reproduced here in spirit.

docs/12 §3 ruling: take the regexes, not the dependency. Both upstreams are dead, so a
dep buys ongoing risk and zero maintenance; the patterns themselves are stable artifacts.
The HTML and scikit-learn paths of talon are deliberately not vendored — Backglass
extracts `text/plain` parts and strips there.

Why this matters at all, per docs/07 §Gmail: stripping happens *before* hashing, so a
forty-message thread does not produce forty near-identical source_items, each paying for
a model call over text that was already read.
"""

from __future__ import annotations

import re

__all__ = ["clean", "strip_quoted", "strip_signature"]

#: talon's SPLITTER_MAX_LINES. An "On <date>, <name> wrote:" attribution wraps, sometimes
#: over several lines, and the address in it can be long. Anything wider than this window
#: is not an attribution line, it is prose that happens to contain both words.
_SPLITTER_MAX_LINES = 6

#: Each pattern marks the point where the message stops and the history begins. The
#: earliest match in the body wins; everything from there down is quoted.
_QUOTE_MARKERS: tuple[re.Pattern[str], ...] = (
    # "On Fri, Jul 10, 2026 at 9:15 AM Dana Whitfield <d@example.gov> wrote:", tolerant of
    # the wrapping Gmail and Apple Mail apply to it.
    re.compile(
        r"^[ \t>]*On\b(?:[^\n]*\n){0," + str(_SPLITTER_MAX_LINES - 1) + r"}?[^\n]*"
        r"\bwrote:[ \t]*$",
        re.MULTILINE,
    ),
    # Outlook, English UI.
    re.compile(
        r"^[ \t>]*-{2,}\s*Original Message\s*-{2,}[ \t]*$",
        re.MULTILINE | re.IGNORECASE,
    ),
    re.compile(
        r"^[ \t>]*-{2,}\s*Forwarded message\s*-{2,}[ \t]*$",
        re.MULTILINE | re.IGNORECASE,
    ),
    # The Outlook header block: From: … (To:/Cc: may intervene) … Sent:/Date: …
    re.compile(
        r"^[ \t>]*From:[^\n]*\n(?:[^\n]*\n){0,3}?[ \t>]*(?:Sent|Date):[^\n]*$",
        re.MULTILINE | re.IGNORECASE,
    ),
    # Outlook Web and some corporate gateways separate the history with a rule of
    # underscores.
    re.compile(r"^_{5,}[ \t]*$", re.MULTILINE),
)

#: A line that is nothing but quoted history.
_QUOTED_LINE = re.compile(r"^[ \t]*>")

#: RFC 3676's separator (`-- `), email-reply-parser's `__` variant, and the trailing-name
#: form talon accepts (`--Marcus`). A bare rule of dashes or underscores counts too.
_SIG_DELIMITER = re.compile(
    r"^[ \t]*(?:-{2,}|_{2,})[ \t]*(?:[A-Za-z][A-Za-z .']{0,40})?$",
)

#: The mobile-client footers. email-reply-parser's SIG_REGEX, widened past the iPhone.
_SIG_SENT_FROM = re.compile(
    r"^[ \t]*(?:Sent|Get)\s+(?:from|Outlook|BlueMail)\b.*$", re.IGNORECASE
)

#: talon's bruteforce sign-offs. On their own line, they open a signature — but only when
#: what follows looks like a name block rather than more message. See _signature_start.
_SIG_SIGN_OFF = re.compile(
    r"^[ \t]*(?:thanks|thank you|thanks again|many thanks|regards|best regards|"
    r"kind regards|warm regards|best|cheers|sincerely|yours|warmly|talk soon|"
    r"speak soon|all the best)[\s,.!—-]*$",
    re.IGNORECASE,
)

#: talon's SIGNATURE_MAX_LINES. A signature lives at the bottom; a "Thanks," forty lines
#: from the end is a sentence.
_SIGNATURE_MAX_LINES = 11

#: How much may follow a sign-off and still be a signature rather than more message.
_SIGN_OFF_TAIL_LINES = 5
_SIGN_OFF_TAIL_WIDTH = 60


def strip_quoted(text: str) -> str:
    """Drop quoted history: attribution lines, `>`-prefixed blocks, forwarded headers."""
    if not text:
        return ""

    cut = len(text)
    for marker in _QUOTE_MARKERS:
        found = marker.search(text)
        if found is not None and found.start() < cut:
            cut = found.start()

    kept = [line for line in text[:cut].splitlines() if not _QUOTED_LINE.match(line)]
    return "\n".join(kept).strip()


def strip_signature(text: str) -> str:
    """Drop the trailing signature block.

    Never returns empty: a message that is nothing but "Thanks, Marcus" is short, not
    absent, and deleting it would lose the only evidence there was.
    """
    if not text:
        return ""

    lines = text.splitlines()
    start = _signature_start(lines)
    if start is None:
        return text.strip()
    head = "\n".join(lines[:start]).strip()
    return head or text.strip()


def clean(text: str) -> str:
    """Quoted history out, then the signature. What is left is what this person wrote."""
    return strip_signature(strip_quoted(text))


def _signature_start(lines: list[str]) -> int | None:
    """Index of the first line belonging to the signature, or None."""
    first_candidate = max(0, len(lines) - _SIGNATURE_MAX_LINES)
    for index in range(first_candidate, len(lines)):
        line = lines[index]
        if _SIG_DELIMITER.match(line) or _SIG_SENT_FROM.match(line):
            return index
        if _SIG_SIGN_OFF.match(line) and _is_name_block(lines[index + 1 :]):
            return index
    return None


def _is_name_block(rest: list[str]) -> bool:
    """Is what follows a sign-off a name and contact details, or is it more message?

    talon cuts at the sign-off unconditionally. That is wrong for us: "Thanks," on its own
    line in the middle of a paragraph would take a commitment with it. Requiring the tail
    to be short and narrow keeps the false positives down, and a residual signature costs
    a few tokens rather than a lost obligation.
    """
    tail = [line for line in rest if line.strip()]
    if not tail:
        return True
    if len(tail) > _SIGN_OFF_TAIL_LINES:
        return False
    return all(len(line.strip()) <= _SIGN_OFF_TAIL_WIDTH for line in tail)
