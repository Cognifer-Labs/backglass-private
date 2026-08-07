#!/usr/bin/env python3
"""One truth per fact. Find the copies that disagree, and say which way the fix runs.

A fact written down twice is a fact that will eventually be written down two different ways.
This repo learned that four times in two days: `tokens.json` carried a paper colour the
stylesheet had abandoned, `tasks/plan.md` stated a retired design rule, a comment cited an
exemption its own commit had removed, and the morning brief's chip radius sat at 4px against
a stylesheet that had moved to 6px. Every one of those was green in CI the whole time.

The reason they survived is worth stating plainly, because it is the thing this tool exists
to break: **a checker keyed to a mirror cannot detect drift in that mirror.** `test_brief.py`
asserted `border-radius:{render.RADIUS_CHIP}` — the constant checking itself. Agreement
between two copies can be wrong in both at once. Only a value re-read from its declared
authority is worth anything.

So: every registered fact names one AUTHORITY and any number of MIRRORS. On disagreement the
tool asks git which line was written last, and that decides the direction of the repair:

    values agree ............................ OK
    mirror's line is OLDER than authority ... STALE  → --fix rewrites the mirror
    mirror's line is NEWER than authority ... ASK    → refuse, exit non-zero, name it

"Removed" means corrected to the authority's value, not deleted — taking it literally would
delete the design system's own tables. "Ask the user" means the tool will not guess: a mirror
edited more recently than the thing it mirrors is a human's fresh intent, and the machine
does not get to overrule it.

The escalation loop is the whole mechanism. If the newer mirror turns out to be right, you do
not fix the mirror — you update the AUTHORITY and run again. Every other copy then goes STALE
and `--fix` repairs all of them in one pass. One decision, propagated.

Usage:
    uv run python scripts/truth.py          # report; non-zero if anything needs attention
    uv run python scripts/truth.py --fix    # repair STALE mirrors; ASK still exits non-zero
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ExtractionError(RuntimeError):
    """A pattern stopped matching.

    This is a hard failure, never a skip. A table gets reformatted, the regex quietly finds
    nothing, and the tool reports the fact clean while being blind to it — which is worse
    than having no tool, because it is green. If you are reading this because the tool
    exited on your reformat: update the pattern, do not delete the fact.
    """


@dataclass(frozen=True)
class Site:
    """One place a value is written down.

    `occurrences` decides what a repeated match means, and it has to be per-site because
    both answers are correct somewhere. `wordmark.svg` paints the paper colour eight times
    and every one of them is the same fact, so all eight must agree and all eight must be
    repaired. `preview.html` contains that same hex thirteen times — but as `--paper`,
    `--on-ink`, and as `--ink`/`--rule` inside the dark blocks, which are different facts
    that merely share a value in one theme. Forcing "all" there would bind unrelated tokens
    together forever. Default is "first"; opt into "all" when the repeats are genuinely one
    fact restated.
    """

    path: str
    pattern: str
    #: Human label for the report; defaults to the path.
    label: str | None = None
    #: "first" — check one match (see `nth`). "all" — every match must agree, --fix repairs all.
    occurrences: str = "first"
    #: Which match, when `occurrences` is "first". Needed because the theme aliases name the
    #: same custom property twice with different meanings: `--ink-2` is neutral 700 in the
    #: light block and neutral 200 in the dark one. They are two facts sharing a spelling, so
    #: position is the only thing that tells them apart without parsing CSS properly.
    nth: int = 0

    def describe(self) -> str:
        return self.label or self.path

    def _text(self) -> str:
        target = ROOT / self.path
        if not target.exists():
            raise ExtractionError(
                f"{self.path}: registered site does not exist.\n"
                f"  Either the file moved (update the registry) or a fact lost one of its "
                f"homes (drop the site deliberately). Silence is not an option here."
            )
        return target.read_text()

    def read_all(self) -> list[tuple[str, int]]:
        """Every (value, 1-indexed line) this site's capture group matches."""
        text = self._text()
        found = [
            (m.group(1), text[: m.start(1)].count("\n") + 1)
            for m in re.finditer(self.pattern, text, re.M)
        ]
        if not found:
            raise ExtractionError(
                f"{self.path}: pattern found nothing.\n"
                f"    {self.pattern}\n"
                f"  The file changed shape. Fix the pattern — a fact that cannot be read "
                f"is not a fact that is correct."
            )
        if self.occurrences == "all":
            return found
        if self.nth >= len(found):
            raise ExtractionError(
                f"{self.path}: wanted match #{self.nth} of {self.pattern!r} "
                f"but only {len(found)} exist. The file lost an occurrence — that is a "
                f"fact quietly disappearing, which is why this raises."
            )
        return [found[self.nth]]

    def read(self) -> tuple[str, int]:
        """The first (value, line). Kept for authorities, which are always single-valued."""
        return self.read_all()[0]

    def disagreeing(self, expected: str, normalise: str) -> list[tuple[str, int]]:
        """The matches that do NOT equal `expected`, under the fact's normaliser."""
        return [
            (value, line)
            for value, line in self.read_all()
            if _norm(value, normalise) != _norm(expected, normalise)
        ]

    def rewrite(self, new_value: str) -> None:
        """Replace the captured spans, leaving every other byte alone.

        Right-to-left, because rewriting left-to-right invalidates every later span the
        moment the replacement differs in length from what it replaced.
        """
        text = self._text()
        spans = [m.span(1) for m in re.finditer(self.pattern, text, re.M)]
        if not spans:  # pragma: no cover — read_all() would have raised first
            raise ExtractionError(f"{self.path}: pattern found nothing on rewrite")
        if self.occurrences != "all":
            spans = [spans[self.nth]]
        for start, end in reversed(spans):
            text = text[:start] + new_value + text[end:]
        (ROOT / self.path).write_text(text)


@dataclass(frozen=True)
class Fact:
    name: str
    authority: Site
    mirrors: tuple[Site, ...] = field(default_factory=tuple)
    #: Some mirrors legitimately spell the value differently (a CSS "6px" vs a JSON 6).
    #: The normaliser is applied to both sides before comparison, never before rewriting.
    normalise: str = "identity"


def _norm(value: str, how: str) -> str:
    if how == "identity":
        return value.strip()
    if how == "hex":
        return value.strip().upper()
    if how == "px":
        return value.strip().removesuffix("px")
    raise ValueError(f"unknown normaliser {how!r}")


def _line_written_at(path: str, line: int) -> int | None:
    """Author time of the commit that last touched this line, or None if uncommitted.

    Per-line, not per-file: a file touched for an unrelated reason must not read as recent.
    None means the line has uncommitted changes, which counts as newest — an edit you have
    not committed is the freshest statement of intent in the tree.
    """
    result = subprocess.run(
        ["git", "blame", "-L", f"{line},{line}", "--porcelain", "--", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    head = result.stdout.split("\n", 1)[0]
    if head.startswith("0000000000000000"):  # not committed yet
        return None
    for row in result.stdout.splitlines():
        if row.startswith("author-time "):
            return int(row.split()[1])
    return None


OK, STALE, ASK, ORPHAN = "OK", "STALE", "ASK", "ORPHAN"


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return result.stdout.splitlines()


def _is_distinctive(value: str) -> bool:
    """Is this value specific enough to search for as a bare literal?

    A hex colour is. The number 6 is not — searching for it would match every line in the
    repo and drown the report, so geometry facts are checked only where they are registered.
    The orphan scan is a net for values that carry their own identity.
    """
    return bool(re.fullmatch(r"#[0-9A-Fa-f]{6}", value.strip())) or len(value.strip()) >= 6


def find_orphans(facts: list[Fact], ignore: tuple[str, ...]) -> list[Finding]:
    """Files that state an authority's value without being registered as a mirror.

    This is the hole every registry has: it only checks what somebody remembered to add.
    A new hardcode in a new file is invisible to a pure registry check, and invisible is how
    all four of this repo's drifts survived. So rather than trusting the registry to be
    complete, go the other way — take the value the authority declares, look for it across
    every tracked file, and flag anything holding a copy that nobody registered.

    File-level on purpose. If a file has even one registered site for the fact, its author
    is assumed to have thought about it, and within-file completeness is the `occurrences`
    setting's job. What this catches is a whole file nobody linked up.
    """
    findings: list[Finding] = []
    tracked = _tracked_files()
    for fact in facts:
        value, _ = fact.authority.read()
        if not _is_distinctive(value):
            continue
        known = {fact.authority.path} | {m.path for m in fact.mirrors}
        for path in tracked:
            if path in known or any(fnmatch(path, pattern) for pattern in ignore):
                continue
            try:
                text = (ROOT / path).read_text()
            except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
                continue
            if value in text:
                line = text[: text.index(value)].count("\n") + 1
                findings.append(
                    Finding(
                        fact.name,
                        Site(path, "", f"{path}:{line}"),
                        ORPHAN,
                        value,
                        value,
                        "holds this value but is not a registered mirror",
                    )
                )
    return findings


@dataclass
class Finding:
    fact: str
    site: Site
    verdict: str
    authority_value: str
    mirror_value: str
    detail: str = ""


def _classify(mirror_at: int | None, truth_at: int | None) -> tuple[str, str]:
    """Which way does the repair run?

    None means "uncommitted", which sorts newest — an edit you have not committed is the
    freshest statement of intent in the tree. Both uncommitted is genuinely ambiguous and
    gets its own message rather than being reported as though only the mirror moved; the
    honest answer there is that a human is mid-edit and the machine should keep its hands
    off. Equal timestamps mean one commit set the two to different values, which is a real
    inconsistency introduced atomically — the authority wins, but the report says so
    plainly instead of implying the mirror is old.
    """
    if mirror_at is None and truth_at is None:
        return ASK, "both the mirror and the authority have uncommitted changes"
    if mirror_at is None:
        return ASK, "mirror has uncommitted changes"
    if truth_at is None:
        return STALE, "authority has uncommitted changes, so the mirror predates it"
    if mirror_at > truth_at:
        return ASK, "mirror's line is newer than the authority's"
    if mirror_at == truth_at:
        return STALE, "same commit set both, to different values — the authority wins"
    return STALE, "mirror's line predates the authority's"


def check(facts: list[Fact]) -> list[Finding]:
    findings: list[Finding] = []
    for fact in facts:
        truth, truth_line = fact.authority.read()
        truth_at: int | None = None
        blamed_authority = False
        for mirror in fact.mirrors:
            disagreeing = mirror.disagreeing(truth, fact.normalise)
            if not disagreeing:
                findings.append(Finding(fact.name, mirror, OK, truth, mirror.read()[0]))
                continue
            # Blame is a subprocess per call, so only pay for it once a fact is in dispute.
            if not blamed_authority:
                truth_at = _line_written_at(fact.authority.path, truth_line)
                blamed_authority = True
            value, line = disagreeing[0]
            verdict, detail = _classify(_line_written_at(mirror.path, line), truth_at)
            if len(disagreeing) > 1:
                detail += f" ({len(disagreeing)} of its occurrences disagree)"
            findings.append(Finding(fact.name, mirror, verdict, truth, value, detail))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="rewrite STALE mirrors")
    args = parser.parse_args()

    from truth_registry import FACTS, ORPHAN_IGNORE  # noqa: PLC0415 — kept beside this file

    findings = check(FACTS)
    orphans = find_orphans(FACTS, ORPHAN_IGNORE)
    stale = [f for f in findings if f.verdict == STALE]
    ask = [f for f in findings if f.verdict == ASK]

    print(f"\n{len(FACTS)} facts, {len(findings)} mirrors checked, "
          f"{len(_tracked_files())} tracked files scanned for unregistered copies\n")

    if args.fix:
        for finding in stale:
            finding.site.rewrite(finding.authority_value)
            print(f"  FIXED  {finding.fact}: {finding.site.describe()} "
                  f"{finding.mirror_value!r} -> {finding.authority_value!r}")
        if stale:
            print()
        stale = []

    for finding in ask:
        print(f"  ASK    {finding.fact}")
        print(f"         authority says {finding.authority_value!r}")
        print(f"         {finding.site.describe()} says {finding.mirror_value!r}")
        print(f"         {finding.detail} — not auto-fixing.")
        print("         If the mirror is right, change the AUTHORITY and re-run;")
        print("         every other copy then goes STALE and --fix repairs them all.\n")

    for finding in stale:
        print(f"  STALE  {finding.fact}: {finding.site.describe()} "
              f"has {finding.mirror_value!r}, authority says {finding.authority_value!r}")
    if stale:
        print("\n  Run with --fix to correct these.\n")

    for finding in orphans:
        print(f"  ORPHAN {finding.fact}")
        print(f"         {finding.site.describe()} holds {finding.authority_value!r}")
        print("         but is not a registered mirror, so nothing keeps it in step.")
        print("         Register it, or replace the literal with the token.\n")

    if not stale and not ask and not orphans:
        print("  All mirrors agree with their authority, and no unregistered copies.\n")
        return 0
    return 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
