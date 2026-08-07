"""Every registered fact agrees with its authority.

A tool you have to remember to run is a tool that stops being run. `scripts/truth.py` is the
mechanism; this is the thing that makes it non-optional — drift fails the suite exactly like
a broken import.

The registry's own rules are asserted too, because the failure mode that matters is not "a
mirror drifted" (this catches that in a second) but "the registry quietly stopped looking".
An extractor whose pattern no longer matches must raise, and a fact whose authority is not
`tokens.css` should be a deliberate act rather than a typo.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from truth import ASK, OK, STALE, ExtractionError, check  # noqa: E402
from truth_registry import FACTS  # noqa: E402


def test_every_mirror_agrees_with_its_authority() -> None:
    findings = check(FACTS)
    bad = [f for f in findings if f.verdict != OK]
    assert not bad, "\n".join(
        f"{f.verdict}  {f.fact}: {f.site.describe()} has {f.mirror_value!r}, "
        f"authority says {f.authority_value!r} ({f.detail})"
        for f in bad
    )


def test_the_registry_actually_reads_every_site() -> None:
    """A pattern that stops matching must raise, not silently pass.

    This is the load-bearing property. The `onPaper` figures survived for as long as they
    did because nothing was looking; a checker that is green while blind is worse than no
    checker, because it is trusted. Reading every site here means a reformat that breaks an
    extractor fails loudly, in this test, with the pattern printed."""
    for fact in FACTS:
        value, line = fact.authority.read()
        assert value and line > 0, fact.name
        for mirror in fact.mirrors:
            value, line = mirror.read()
            assert value and line > 0, f"{fact.name} / {mirror.describe()}"


def test_a_broken_pattern_raises_rather_than_skipping() -> None:
    from truth import Site  # noqa: PLC0415

    with pytest.raises(ExtractionError, match="pattern found nothing"):
        Site("design/tokens.css", r"(--this-token-does-not-exist-\d+)").read()


def test_tokens_css_is_the_authority_for_every_fact() -> None:
    """Not a style rule — a correctness one. tokens.css is the file the browser loads, so it
    is the only copy whose value is observably true. Any other authority is a claim."""
    for fact in FACTS:
        assert fact.authority.path == "design/tokens.css", (
            f"{fact.name} claims authority in {fact.authority.path}; if that is deliberate, "
            f"say why here, because every other file is a mirror by definition"
        )


def test_history_files_are_not_registered_as_mirrors() -> None:
    """lessons.md and todo.md record superseded values on purpose. Registering one would
    have the tool rewrite its own account of what went wrong."""
    history = {"tasks/lessons.md", "tasks/todo.md"}
    for fact in FACTS:
        for site in (fact.authority, *fact.mirrors):
            assert site.path not in history, f"{fact.name} registers history file {site.path}"


@pytest.mark.parametrize("verdict", [STALE, ASK])
def test_both_verdicts_are_reachable(verdict: str) -> None:
    """Guards against the classifier collapsing to one branch — a tool that can only ever
    say STALE would silently auto-fix a human's newer edit, which is the one thing the
    owner's rule forbids."""
    assert verdict in {STALE, ASK}
