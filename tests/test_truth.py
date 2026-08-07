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


def test_no_unregistered_copies_of_any_registered_value() -> None:
    """The hole every registry has: it only checks what somebody remembered to add.

    A pure registry check is blind to a brand-new hardcode in a brand-new file, and blind is
    how all four of this repo's drifts survived. So the scan runs the other way — take the
    value the authority declares, look for it across every tracked file, flag anything
    holding a copy nobody registered. It found four on its first run, including the reel's
    three literal paper hexes and every ink inside the palette validator."""
    from truth import find_orphans  # noqa: PLC0415
    from truth_registry import ORPHAN_IGNORE  # noqa: PLC0415

    orphans = find_orphans(FACTS, ORPHAN_IGNORE)
    assert not orphans, "\n".join(
        f"{f.site.describe()} holds {f.authority_value!r} for '{f.fact}' but is unregistered"
        for f in orphans
    )


# ── the edge cases the mechanism has to survive ───────────────────────────────


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Point the tool at a scratch tree so rewrite tests cannot touch the real files."""
    import truth  # noqa: PLC0415

    monkeypatch.setattr(truth, "ROOT", tmp_path)
    return tmp_path


def test_all_occurrences_catches_a_partial_drift(sandbox: Path) -> None:
    """The wordmark paints paper eight times. Checking one would have let the other seven
    rot — which is exactly what happened before any of this existed."""
    from truth import Site  # noqa: PLC0415

    (sandbox / "mark.svg").write_text(
        'fill="#FCF8EC" fill="#FCF8EC" fill="#BADBAD" fill="#FCF8EC"'
    )
    site = Site("mark.svg", r'fill="(#[0-9A-Fa-f]{6})"', occurrences="all")
    assert len(site.read_all()) == 4
    assert site.disagreeing("#FCF8EC", "hex") == [("#BADBAD", 1)]
    # And a first-match site would have declared the same file clean.
    assert not Site("mark.svg", r'fill="(#[0-9A-Fa-f]{6})"').disagreeing("#FCF8EC", "hex")


def test_fix_repairs_every_occurrence_without_corrupting_spans(sandbox: Path) -> None:
    """Rewriting left-to-right invalidates every later span the moment the replacement
    differs in length. This asserts the right-to-left pass with a deliberately longer value."""
    from truth import Site  # noqa: PLC0415

    (sandbox / "mark.svg").write_text('a="#111111" b="#111111" c="#111111"')
    Site("mark.svg", r'"(#[0-9A-Fa-f]{6})"', occurrences="all").rewrite("#22222222")
    assert (sandbox / "mark.svg").read_text() == 'a="#22222222" b="#22222222" c="#22222222"'


def test_nth_tells_the_two_themes_apart(sandbox: Path) -> None:
    """`--ink-2` is neutral 700 in the light block and neutral 200 in the dark one. Same
    spelling, two facts; position is the only thing separating them short of a CSS parser."""
    from truth import Site  # noqa: PLC0415

    (sandbox / "t.css").write_text(":root{--ink-2:#3d3c37;}\n@media dark{--ink-2:#c9c4b5;}\n")
    pattern = r"--ink-2:(#[0-9A-Fa-f]{6});"
    assert Site("t.css", pattern, nth=0).read()[0] == "#3d3c37"
    assert Site("t.css", pattern, nth=1).read()[0] == "#c9c4b5"


def test_a_lost_occurrence_raises_rather_than_silently_shifting(sandbox: Path) -> None:
    """If the dark block is deleted, `nth=1` must fail loudly. Falling back to the light
    value would silently start checking the wrong theme and report it green."""
    from truth import ExtractionError, Site  # noqa: PLC0415

    (sandbox / "t.css").write_text(":root{--ink-2:#3d3c37;}\n")
    with pytest.raises(ExtractionError, match="only 1 exist"):
        Site("t.css", r"--ink-2:(#[0-9A-Fa-f]{6});", nth=1).read()


def test_a_missing_file_raises_rather_than_skipping(sandbox: Path) -> None:
    from truth import ExtractionError, Site  # noqa: PLC0415

    with pytest.raises(ExtractionError, match="does not exist"):
        Site("gone.css", r"(x)").read()


@pytest.mark.parametrize(
    ("mirror_at", "truth_at", "expected"),
    [
        (100, 200, STALE),  # mirror older — repair it
        (200, 100, ASK),  # mirror newer — a human's fresh intent, do not overrule
        (None, 100, ASK),  # mirror uncommitted — newest of all
        (100, None, STALE),  # authority uncommitted — the mirror predates it
        (None, None, ASK),  # both mid-edit — ambiguous, keep hands off
        (100, 100, STALE),  # one commit set both differently — the authority wins
    ],
)
def test_every_recency_branch_classifies_as_specified(
    mirror_at: int | None, truth_at: int | None, expected: str
) -> None:
    """The owner's rule is the whole contract: older loses, newer asks. A classifier that
    collapsed to STALE would silently overwrite a human edit, which is the one outcome
    forbidden — so every branch is pinned, including the two ambiguous ones."""
    from truth import _classify  # noqa: PLC0415

    verdict, detail = _classify(mirror_at, truth_at)
    assert verdict == expected, detail
    assert detail, "every verdict must explain itself in the report"
