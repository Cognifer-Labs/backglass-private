"""design/tokens.json must agree with design/tokens.css, and its contrast figures must be
arithmetically true.

Written 2026-08-07, after finding that `tokens.json` still declared paper as `#FAF3DF` — an
earlier colour — while `tokens.css` shipped `#FCF8EC`. Nothing reads `tokens.json`, so the
drift was silent and total: **every** published `onPaper` ratio in it, and in
design-system.md, had been computed against the old paper and matched it to the last
decimal. `scripts/validate-palette.mjs` hard-codes the real paper and had been passing
against the true values the whole time, so the two halves of the design system disagreed
for however long without anything failing.

A mirror nobody reads is exactly the artefact that rots, which is why the check has to be
mechanical rather than a promise to re-run the validator. Two kinds of assertion here:

  - the hexes in the JSON are the hexes in the CSS (agreement), and
  - the ratios in the JSON are what the WCAG formula actually returns (truth).

The second is the one that matters. A figure that is merely *consistent* with a sibling
file can still be wrong in both; a figure recomputed from its own hex cannot.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

DESIGN = Path(__file__).resolve().parent.parent / "design"
TOKENS_JSON = json.loads((DESIGN / "tokens.json").read_text())
TOKENS_CSS = (DESIGN / "tokens.css").read_text()


def _css_var(name: str) -> str:
    """The light-theme value of a custom property, as authored."""
    match = re.search(rf"^\s*--{re.escape(name)}:\s*([^;]+);", TOKENS_CSS, re.M)
    assert match, f"--{name} not found in tokens.css"
    return match.group(1).strip()


def _relative_luminance(hex_colour: str) -> float:
    """WCAG 2.x relative luminance."""
    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))

    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


PAPER = TOKENS_JSON["surfaces"]["paper"]["hex"]
BLACK = TOKENS_JSON["surfaces"]["black"]["hex"]


def test_the_surfaces_match_the_stylesheet() -> None:
    """The drift that started this: tokens.json said #FAF3DF, tokens.css shipped #FCF8EC."""
    assert PAPER.upper() == _css_var("paper").upper()
    assert BLACK.upper() == _css_var("ink").upper()


def test_every_ink_matches_the_stylesheet() -> None:
    for name, ink in TOKENS_JSON["inks"].items():
        if name.startswith("$"):
            continue
        assert ink["hex"].upper() == _css_var(name).upper(), name


def test_every_neutral_matches_the_stylesheet() -> None:
    for step, neutral in TOKENS_JSON["neutrals"].items():
        if step.startswith("$"):
            continue
        assert neutral["hex"].upper() == _css_var(f"n-{step}").upper(), step


@pytest.mark.parametrize("group", ["neutrals", "inks"])
def test_published_contrast_figures_are_arithmetically_true(group: str) -> None:
    """Recomputed from each entry's own hex, not compared against a sibling file. A figure
    that is only consistent with its mirror can be wrong in both places at once — which is
    precisely what happened."""
    for name, entry in TOKENS_JSON[group].items():
        if name.startswith("$"):
            continue
        for key, surface in (("onPaper", PAPER), ("onBlack", BLACK)):
            if key not in entry:
                continue
            actual = round(_contrast(entry["hex"], surface), 2)
            assert abs(entry[key] - actual) < 0.011, (
                f"{group}.{name}.{key} published {entry[key]}, actual {actual} "
                f"({entry['hex']} on {surface})"
            )


def test_the_paper_surface_figure_is_true() -> None:
    published = TOKENS_JSON["surfaces"]["paper"]["vsBlack"]
    actual = round(_contrast(PAPER, BLACK), 2)
    assert abs(published - actual) < 0.011, f"published {published}, actual {actual}"


def test_the_geometry_block_matches_the_radius_scale() -> None:
    """tokens.json carried [2, 4, 6] for a day after the scale became 3/6/10."""
    scale = [_css_var(f"radius-{n}") for n in (1, 2, 3)]
    assert [f"{v}px" for v in TOKENS_JSON["geometry"]["radius"]] == scale
    assert f"{TOKENS_JSON['geometry']['radiusReel']}px" == scale[0]
    assert f"{TOKENS_JSON['geometry']['border']}px" == _css_var("border")


def test_the_wordmark_uses_the_system_paper() -> None:
    """It shipped the old cream, so the mark would have sat as a different beige against
    the page it is printed on."""
    wordmark = (DESIGN / "wordmark.svg").read_text()
    stale = re.findall(r"#[0-9A-Fa-f]{6}", wordmark)
    allowed = {PAPER.upper(), BLACK.upper()} | {
        ink["hex"].upper() for name, ink in TOKENS_JSON["inks"].items() if not name.startswith("$")
    }
    for colour in stale:
        assert colour.upper() in allowed, f"wordmark.svg uses {colour}, not a system colour"
