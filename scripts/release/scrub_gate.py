"""Second, independent net against personal-identifier leakage in an assembled
export tree. The allowlist in `manifest.py` is the primary boundary — most
personal content never enters the export candidate set — but this catches
whatever leaked *into* an allowed file. Concretely demonstrated during planning:
a full-name/email grep alone missed a bare GitHub-fixture-username class
(`kesavan` in `tests/test_github.py`, 17 hits) that this gate's own term list
now covers.

Usage:
    uv run python scripts/release/scrub_gate.py <tree-dir>

Exits non-zero and prints `file:line:term` for every hit if anything is found.
Exits 0 (silently) if the tree is clean.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Kept as a flat constant so it's trivially extendable later without touching
# the walking logic below. Three matching modes, because naive case-insensitive
# substring matching produces false positives on short/common terms (e.g. "ASU"
# inside "measure", "casual") and false negatives on missed classes.
WORD_BOUNDED_TERMS = [
    "ASU",
    "Cognifer",
    "Kesavan",
    "McKenna",
    "Gathas",
    "Nyasha",
    "Sheppard",
]
SUBSTRING_TERMS = [
    "Dharsan",
    "contactdharsan",
    "dkesava2",
    "kesavand",
    "/Users/Dharsan",
]
LITERAL_CASE_SENSITIVE_TERMS = [
    "Arizona State",
]

# The one narrow exemption, per the owner's explicit 2026-08-02 answer: the MIT
# LICENSE copyright line is allowed to carry the owner's legal name. Nothing
# else — not another line in LICENSE, not any other file — is exempt, even if
# it also matches "Dharsan" or "Kesavan".
LICENSE_EXEMPT_FILENAME = "LICENSE"
LICENSE_EXEMPT_LINE_RE = re.compile(r"^Copyright \(c\) \d{4} Dharsan Kesavan$")

# Extensions unlikely to be meaningfully text-scannable (fonts, binary blobs).
# Everything else is attempted as UTF-8 text; a decode failure is treated as
# binary and skipped rather than crashing the gate.
SKIP_EXTENSIONS = {".woff2", ".woff", ".ttf", ".otf", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".db"}

_WORD_BOUNDED_RE = [
    (term, re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE))
    for term in WORD_BOUNDED_TERMS
]
_SUBSTRING_RE = [
    (term, re.compile(re.escape(term), re.IGNORECASE)) for term in SUBSTRING_TERMS
]
_LITERAL_RE = [
    (term, re.compile(re.escape(term))) for term in LITERAL_CASE_SENSITIVE_TERMS
]


def _iter_text_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if ".git" in path.parts:
            continue
        if path.suffix.lower() in SKIP_EXTENSIONS:
            continue
        yield path


def scan(root: Path) -> list[tuple[str, int, str]]:
    """Return a list of (relative_path, line_no, term) hits."""
    hits: list[tuple[str, int, str]] = []
    for path in _iter_text_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if rel == LICENSE_EXEMPT_FILENAME and LICENSE_EXEMPT_LINE_RE.match(line):
                continue
            for term, pattern in _WORD_BOUNDED_RE:
                if pattern.search(line):
                    hits.append((rel, line_no, term))
            for term, pattern in _SUBSTRING_RE:
                if pattern.search(line):
                    hits.append((rel, line_no, term))
            for term, pattern in _LITERAL_RE:
                if pattern.search(line):
                    hits.append((rel, line_no, term))
    return hits


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: scrub_gate.py <tree-dir>", file=sys.stderr)
        return 2
    root = Path(argv[1]).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2
    hits = scan(root)
    if hits:
        for rel, line_no, term in hits:
            print(f"{rel}:{line_no}:{term}")
        print(f"\nscrub_gate: {len(hits)} banned-term hit(s) found.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
