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
    """One place a value is written down."""

    path: str
    pattern: str
    #: Human label for the report; defaults to the path.
    label: str | None = None

    def describe(self) -> str:
        return self.label or self.path

    def read(self) -> tuple[str, int]:
        """Return (value, 1-indexed line number) for this site's single capture group."""
        text = (ROOT / self.path).read_text()
        match = re.search(self.pattern, text, re.M)
        if match is None:
            raise ExtractionError(
                f"{self.path}: pattern found nothing.\n"
                f"    {self.pattern}\n"
                f"  The file changed shape. Fix the pattern — a fact that cannot be read "
                f"is not a fact that is correct."
            )
        line = text[: match.start(1)].count("\n") + 1
        return match.group(1), line

    def rewrite(self, new_value: str) -> None:
        """Replace just the captured span, leaving every other byte alone."""
        target = ROOT / self.path
        text = target.read_text()
        match = re.search(self.pattern, text, re.M)
        if match is None:  # pragma: no cover — read() would have raised first
            raise ExtractionError(f"{self.path}: pattern found nothing on rewrite")
        start, end = match.span(1)
        target.write_text(text[:start] + new_value + text[end:])


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


OK, STALE, ASK = "OK", "STALE", "ASK"


@dataclass
class Finding:
    fact: str
    site: Site
    verdict: str
    authority_value: str
    mirror_value: str
    detail: str = ""


def check(facts: list[Fact]) -> list[Finding]:
    findings: list[Finding] = []
    for fact in facts:
        truth, truth_line = fact.authority.read()
        truth_at = _line_written_at(fact.authority.path, truth_line)
        for mirror in fact.mirrors:
            value, line = mirror.read()
            if _norm(value, fact.normalise) == _norm(truth, fact.normalise):
                findings.append(Finding(fact.name, mirror, OK, truth, value))
                continue
            mirror_at = _line_written_at(mirror.path, line)
            # Uncommitted (None) sorts newest on either side.
            if mirror_at is None:
                verdict, detail = ASK, "mirror has uncommitted changes"
            elif truth_at is None:
                verdict, detail = STALE, "authority has uncommitted changes"
            elif mirror_at > truth_at:
                verdict, detail = ASK, "mirror's line is newer than the authority's"
            else:
                verdict, detail = STALE, "mirror's line predates the authority's"
            findings.append(Finding(fact.name, mirror, verdict, truth, value, detail))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="rewrite STALE mirrors")
    args = parser.parse_args()

    from truth_registry import FACTS  # noqa: PLC0415 — kept beside this file

    findings = check(FACTS)
    stale = [f for f in findings if f.verdict == STALE]
    ask = [f for f in findings if f.verdict == ASK]

    print(f"\n{len(FACTS)} facts, {len(findings)} mirrors checked\n")

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

    if not stale and not ask:
        print("  All mirrors agree with their authority.\n")
        return 0
    return 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
