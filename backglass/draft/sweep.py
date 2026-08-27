"""The tells that say a machine wrote it, found deterministically.

Every draft this app produces goes out under the owner's name, into threads where the
recipient has the owner's previous emails to compare against. One em dash in a reply to
someone who has read six of your plain-hyphen emails is not a style question; it is the
moment they start reading the rest of it differently.

So this module is the gate both drafting paths pass through. `people/reachout.py` renders
templates offline and `draft/reply.py` calls a model, and neither one is trusted to be
clean on its own — the templates because a human wrote a phrase years ago that has been
going out ever since, the model because producing these patterns is what it was trained
to do.

Two functions, and the split between them is the whole design:

  - **`findings(text)` reports.** Every category, every match, with an offset. Nothing is
    changed. This is what the owner reads and what the prompt is told to avoid.
  - **`clean(text)` fixes, and only where fixing cannot be wrong.** Characters — dashes,
    curly quotes, the ellipsis glyph, invisible spaces. A character substitution changes
    how a sentence looks and never what it says, so it can run unattended.

**`clean` deliberately does not touch phrases.** "I hope this email finds you well" is
a tell, and deleting it by regex leaves either a hole or a seam, and deleting the
sentence around it can take a real clause with it. Rewriting a sentence is the model's
job or the owner's; it is not a substitution table's. Phrases are found, named, and
handed to whoever can actually rewrite them.

The same restraint applies to emoji: found, never stripped. An owner who typed a 👍 meant
it, and a sweep that silently deletes what the owner typed is a sweep the owner stops
trusting.

One caveat, borrowed from the Wikipedia AI-cleanup guide these categories come from: the
tells are symptoms, not the disease. Scrubbing an em dash off a paragraph whose facts are
invented makes bad text harder to catch, not better. Nothing here checks whether a claim
is true — `reply.py` carries that burden by refusing to state anything the thread does
not, and by naming every row it read.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

#: Phrases that carry no information and mark the writer as a machine. Found, never
#: deleted — see the module docstring. Written as regex so a phrase can tolerate the
#: small variations it actually appears in ("reach out" / "reaching out").
PHRASES: tuple[tuple[str, str], ...] = (
    (r"i hope this (?:e-?mail|message|note) finds you well", "empty opener"),
    (r"i hope (?:this|things|you)[^.!?]{0,40}\bgoing well", "empty opener"),
    (r"just (?:following up|circling back|checking in)", "guilt nudge"),
    (r"bump(?:ing)? this", "guilt nudge"),
    (r"i know you(?:'|’)?re (?:very )?busy", "guilt nudge"),
    (r"i(?:'|’)?d be happy to", "filler politeness"),
    (r"(?:please )?don(?:'|’)?t hesitate to", "filler politeness"),
    (r"feel free to", "filler politeness"),
    (r"i look forward to hearing (?:from|back)", "filler closer"),
    (r"i wanted to reach out", "filler opener"),
    (r"i(?:'|’)?m reaching out to", "filler opener"),
    (r"as you may (?:know|be aware)", "filler opener"),
    (r"in today(?:'|’)?s [a-z-]+ (?:world|landscape|environment)", "filler opener"),
    (r"thank you for reaching out", "filler opener"),
    (r"as an ai(?: language)? model", "leaked model voice"),
    (r"i hope this helps", "leaked model voice"),
)

#: Vocabulary that clusters in generated prose. A single one of these is not a verdict —
#: `findings` reports them individually and the caller decides on the cluster, which is
#: why they carry their own category rather than being folded in with PHRASES.
INFLATED: tuple[str, ...] = (
    "delve", "tapestry", "testament", "pivotal", "crucial", "underscore",
    "underscores", "underscored", "showcase", "showcases", "showcasing",
    "vibrant", "intricate", "meticulous", "robust", "boasts", "bolstered",
    "enduring", "foster", "fosters", "fostering", "garner", "garners",
    "garnered", "interplay", "landscape", "leverage", "leveraging",
    "navigate", "navigating", "realm", "seamless", "seamlessly",
)

#: Placeholders that mean a template was never filled. These are a hard failure in a
#: draft: an email that reaches a recipient containing "[Your Name]" is worse than no
#: email, so callers are expected to refuse to show a draft that still has one.
PLACEHOLDER = re.compile(
    r"\[(?:your|insert|describe|enter|entity|company|topic|name|link to|recipient)\b[^\]]*\]"
    r"|(?:INSERT|PASTE|SOURCE|TODO)_[A-Z_]+",
    re.IGNORECASE,
)

#: Character substitutions. Each one changes how the text looks and never what it says,
#: which is the property that lets `clean` run without review.
_CHARS: tuple[tuple[str, str], ...] = (
    ("“", '"'), ("”", '"'),          # curly double quotes
    ("‘", "'"), ("’", "'"),          # curly single quotes and apostrophe
    ("…", "..."),                          # ellipsis glyph
    (" ", " "), (" ", " "),          # non-breaking spaces
    ("′", "'"), ("″", '"'),          # prime marks
    ("−", "-"),                            # minus sign used as a hyphen
)

#: Removed outright: they are invisible, so nothing is lost and their only function in a
#: draft is to survive a copy-paste and mark where the text came from.
_INVISIBLE = ("​", "‌", "‍", "﻿", "⁠")

_EM_EN = re.compile(r"\s*[–—]\s*")
_DIGIT_RANGE = re.compile(r"(?<=\d)\s*[–—]\s*(?=\d)")
_MD_EMPHASIS = re.compile(r"(?<!\w)\*\*(?=\S)|(?<=\S)\*\*(?!\w)")


@dataclass(frozen=True)
class Finding:
    """One tell, with enough detail to point at it in the text.

    `offset` is a character index into the text as it was handed in, so a caller can
    highlight the span without re-running the search.
    """

    category: str
    #: What was actually matched, verbatim — never a description of it. The owner
    #: reading the report should see their own words, not a paraphrase of the rule.
    text: str
    offset: int
    #: What to do about it, in one clause. `clean` handles some of these itself; the
    #: rest are for a person or a model.
    remedy: str

    def line(self) -> str:
        return f"{self.category}: {self.text!r} at {self.offset} — {self.remedy}"


def _emoji(text: str) -> Iterable[tuple[int, str]]:
    """Pictographic characters, by Unicode category rather than a hand-kept range.

    A hardcoded codepoint range goes stale with every Unicode release and misses the
    emoji that were added after it was written. `So` (Symbol, other) plus the variation
    selector catches the pictographs without catching the arrows and currency symbols a
    normal email legitimately contains.
    """
    for index, char in enumerate(text):
        if char in "’‘“”":
            continue
        if unicodedata.category(char) == "So" and ord(char) > 0x2100:
            yield index, char


def findings(text: str) -> tuple[Finding, ...]:
    """Every tell in the text, in the order they appear. Changes nothing."""
    text = text or ""
    found: list[Finding] = []

    for pattern, label in PHRASES:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            found.append(
                Finding(
                    category="phrase",
                    text=match.group(0),
                    offset=match.start(),
                    remedy=f"{label} — cut it, or say the specific thing instead",
                )
            )

    for match in PLACEHOLDER.finditer(text):
        found.append(
            Finding(
                category="placeholder",
                text=match.group(0),
                offset=match.start(),
                remedy="unfilled slot — this must not reach a recipient",
            )
        )

    for match in re.finditer(r"[–—]", text):
        found.append(
            Finding(
                category="watermark",
                text=match.group(0),
                offset=match.start(),
                remedy="em/en dash — clean() rewrites it as a plain hyphen",
            )
        )

    for glyph, replacement in _CHARS:
        start = 0
        while (index := text.find(glyph, start)) != -1:
            found.append(
                Finding(
                    category="watermark",
                    text=glyph,
                    offset=index,
                    remedy=f"clean() rewrites it as {replacement!r}",
                )
            )
            start = index + 1

    for glyph in _INVISIBLE:
        start = 0
        while (index := text.find(glyph, start)) != -1:
            found.append(
                Finding(
                    category="watermark",
                    text=f"U+{ord(glyph):04X}",
                    offset=index,
                    remedy="invisible character — clean() removes it",
                )
            )
            start = index + 1

    for index, char in _emoji(text):
        found.append(
            Finding(
                category="emoji",
                text=char,
                offset=index,
                remedy="kept as typed — remove it yourself if you did not mean it",
            )
        )

    for word in INFLATED:
        for match in re.finditer(rf"\b{re.escape(word)}\b", text, re.IGNORECASE):
            found.append(
                Finding(
                    category="inflated",
                    text=match.group(0),
                    offset=match.start(),
                    remedy="generated vocabulary — a plainer word usually says more",
                )
            )

    return tuple(sorted(found, key=lambda f: (f.offset, f.category)))


def clean(text: str) -> str:
    """The mechanical half: characters only, never phrasing.

    Safe to run unattended on any draft, because every substitution here preserves
    meaning exactly. What it will not do is rewrite a sentence — see the module
    docstring for why that restraint is the point rather than a limitation.
    """
    if not text:
        return text or ""

    # Ranges first: "2019–2024" is a joined range and wants a bare hyphen, while a
    # sentence dash wants the spaced hyphen a person would actually type. Doing the
    # general rule first would turn the year range into "2019 - 2024".
    out = _DIGIT_RANGE.sub("-", text)
    out = _EM_EN.sub(" - ", out)

    for glyph, replacement in _CHARS:
        out = out.replace(glyph, replacement)
    for glyph in _INVISIBLE:
        out = out.replace(glyph, "")

    # Markdown bold is markup a model adds and a mail client renders as literal
    # asterisks. Nothing else in the message needs them.
    out = _MD_EMPHASIS.sub("", out)

    # A spaced hyphen at the start of a line is a bullet the owner may have meant;
    # one produced by the dash rule above is a seam. Only the seam is repaired.
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip("\n")


def report(text: str) -> tuple[str, tuple[Finding, ...]]:
    """`clean` and `findings` in one call, in the order a caller wants them.

    Findings are taken from the **cleaned** text so the report describes what the owner
    is about to send, not what the model produced. What `clean` already fixed is gone by
    then; what survives is exactly the list of things a person still has to decide on.
    """
    cleaned = clean(text)
    return cleaned, findings(cleaned)


def is_sendable(text: str) -> bool:
    """False when the text still carries something that must never reach a recipient.

    Only placeholders qualify. A stray "crucial" is a matter of taste and the owner is
    allowed to send it; "[Your Name]" is a bug wearing an email's clothes.
    """
    return not any(f.category == "placeholder" for f in findings(text))


__all__ = [
    "Finding",
    "INFLATED",
    "PHRASES",
    "PLACEHOLDER",
    "clean",
    "findings",
    "is_sendable",
    "report",
]
