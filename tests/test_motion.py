"""design-system.md §9, asserted rather than trusted.

Motion is the easiest layer in a design system to erode, because every violation is
locally reasonable: one component wants 250ms, one wants a spring, one wants to fade
its own way. None of those reads as a mistake in isolation, and the sum of them is a
product with no rhythm. §3 spends five inks and §9 spends five durations for the same
reason, so the same kind of test guards both.

These read the sheet rather than the rendered page on purpose. A transition is not in
the DOM and not in the response body; the only place the rule exists is the stylesheet,
so the stylesheet is what is asked.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "backglass" / "web" / "static" / "dashboard.css"
TOKENS = ROOT / "design" / "tokens.css"
BASE = ROOT / "backglass" / "web" / "templates" / "base.html"


def _sheet() -> str:
    """The stylesheet with its comments removed.

    Comments in this file quote the rules they follow — a `transition` named in a
    paragraph explaining why there is no transition would be read as a declaration by
    every assertion below. `test_no_shadows_no_gradients_outside_the_hatch` learned the
    same lesson from the other end and strips comments for the same reason.
    """
    return re.sub(r"/\*.*?\*/", "", CSS.read_text(), flags=re.S)


#: Everything a `transition` or `animation` shorthand is allowed to name. Timing is
#: always a token; the property list is the §9 rule 1 set plus the colour properties,
#: which are paint-only and cost no layout.
ALLOWED_PROPERTIES = {
    "opacity",
    "transform",
    "background-color",
    "border-color",
    "color",
}


def _declarations(name: str) -> list[str]:
    """Every `transition:`/`animation:` value in the sheet, as written."""
    return [m.group(1).strip() for m in re.finditer(rf"\b{name}:\s*([^;}}]+)", _sheet())]


def test_no_duration_is_written_literally_in_a_component() -> None:
    """§9 rule 3. A component that invents a speed has made a system decision alone.

    This is the rule the pre-§9 sheet broke: `.htmx-swapping` carried a literal `.08s`
    that belonged to no scale — and, as it turned out, never played at all, because
    htmx replaces the outgoing node in the same frame. A literal duration and a
    duration nobody checked tend to be the same duration.
    """
    for value in _declarations("transition") + _declarations("animation"):
        literals = re.findall(r"(?<![\w-])\d*\.?\d+m?s\b", value)
        assert not literals, (
            f"{value!r} writes {literals} literally. §9 gives five motion tokens; "
            f"a component that needs a sixth speed is a system decision, not a "
            f"component one."
        )
        assert "cubic-bezier" not in value, (
            f"{value!r} hand-rolls a curve. The two curves live in tokens.css as "
            f"--ease-out and --ease-in-out."
        )


def test_only_transform_opacity_and_colour_are_animated() -> None:
    """§9 rule 1, and it is a performance rule before it is an aesthetic one.

    `transform` and `opacity` skip layout and paint; `width`, `height`, `top` and the
    box properties trigger all three, on every frame, for the whole subtree. `all` is
    worse than any of them individually because it animates whatever a future edit
    happens to add.
    """
    for value in _declarations("transition"):
        # A shorthand list: "opacity 160ms var(--ease-out), transform 160ms ..."
        for part in re.split(r",(?![^(]*\))", value):
            first = part.strip().split()[0] if part.strip() else ""
            if not first or first.startswith("var("):
                continue
            assert first in ALLOWED_PROPERTIES, (
                f"transition animates {first!r}. §9 rule 1 allows "
                f"{sorted(ALLOWED_PROPERTIES)} — a box property animates layout on "
                f"every frame, and `all` animates whatever the next edit adds."
            )


def test_ease_in_appears_nowhere() -> None:
    """§9's easing rule. `ease-in` starts slow, which delays the exact instant the
    owner is watching; a 200ms ease-in feels slower than a 200ms ease-out.

    `ease-in-out` is a different curve and is allowed — it is for something already on
    screen that moves — so the assertion has to distinguish them rather than grep for
    the substring.
    """
    for source in (CSS, TOKENS):
        stripped = re.sub(r"/\*.*?\*/", "", source.read_text(), flags=re.S)
        offenders = re.findall(r"(?<![\w-])ease-in(?!-out)(?![\w-])", stripped)
        assert not offenders, f"{source.name} uses ease-in; §9 forbids it on UI."


def test_every_duration_is_under_the_ceiling() -> None:
    """§9 rule 2. Three hundred milliseconds is where responsive becomes deliberate,
    and nothing in a status dashboard is deliberate."""
    durations = re.findall(r"--motion(?:-fast|-slow)?:\s*(\d+)ms", TOKENS.read_text())
    assert durations, "the motion durations are gone from tokens.css"
    for value in durations:
        assert int(value) <= 300, f"{value}ms exceeds the §9 ceiling"


def test_reduced_motion_is_settled_at_the_token_layer() -> None:
    """§9 rule 4, and the reason it is a rule rather than a habit.

    Zeroing travel and press at the token layer resolves every transform in the
    product to the identity, so a rule written next year inherits the setting instead
    of having to remember it. The alternative — an override per component — is a list
    that is correct on the day it is written and never again.
    """
    tokens = TOKENS.read_text()
    block = re.search(
        r"@media \(prefers-reduced-motion: reduce\)\s*\{(.+?)\n\}", tokens, flags=re.S
    )
    assert block, "tokens.css no longer zeroes motion for prefers-reduced-motion"
    body = block.group(1)
    assert re.search(r"--motion-travel:\s*0", body), (
        "reduced motion must zero the travel distance — that is what stills every "
        "translate in the product at once"
    )
    assert re.search(r"--motion-press:\s*1\b", body), (
        "reduced motion must return the press depth to 1"
    )
    # Reduced motion means less movement, not less feedback: the cross-fades stay.
    assert "--motion:" not in body and "--motion-fast:" not in body, (
        "the durations are deliberately left alone under reduced motion — an opacity "
        "cross-fade carries no vestibular load and is what keeps a swapped panel from "
        "reading as a page that flickered"
    )


def test_content_arrives_by_starting_style_not_by_an_htmx_class() -> None:
    """The load-bearing choice in the motion layer, so it is pinned.

    htmx removes `htmx-added` and `htmx-settling` twenty milliseconds after the swap,
    so a transition keyed to either animates for twenty milliseconds and then snaps.
    `@starting-style` needs no class: it catches any element newly inserted into the
    document, which is the swapped panel, the swapped fragment and — because a
    navigation inserts a whole document — the page itself. One mechanism, and it is
    why page switching needs no page-transition machinery.
    """
    sheet = _sheet()
    assert "@starting-style" in sheet, "the arrival animation is gone"
    for dead in ("htmx-added", "htmx-settling"):
        assert f".{dead}" not in sheet, (
            f".{dead} is removed 20ms after the swap; a transition keyed to it "
            f"animates for 20ms and snaps"
        )


def _swap_targets() -> dict[str, tuple[str, str]]:
    """Every region a write replaces, mapped to the classes its root element carries.

    Both halves of htmx's vocabulary count: `hx-target` names the region a click
    replaces, and `hx-swap-oob` marks a region replaced by a write aimed somewhere
    else entirely — a checklist tick repaints the week grid without ever naming it.
    """
    found: dict[str, tuple[str, str]] = {}
    templates = sorted((ROOT / "backglass" / "web" / "templates").glob("*.html"))

    ids: set[str] = set()
    for path in templates:
        text = path.read_text()
        ids |= set(re.findall(r'hx-target="#([\w-]+)"', text))
        for match in re.finditer(r'hx-swap-oob="', text):
            # The oob marker sits on the element it replaces, so its id is that
            # element's own — scan back to the opening angle bracket for it.
            head = text.rfind("<", 0, match.start())
            tag = text[head : match.start()]
            own = re.search(r'id="([\w-]+)"', tag)
            if own:
                ids.add(own.group(1))

    for target in ids:
        for path in templates:
            text = path.read_text()
            where = text.find(f'id="{target}"')
            if where < 0:
                continue
            head = text.rfind("<", 0, where)
            tail = text.find(">", where)
            tag = text[head : tail + 1]
            classes = re.search(r'class="([^"]*)"', tag)
            found[target] = (path.name, classes.group(1) if classes else "")
            break
    return found


def test_every_swap_target_arrives() -> None:
    """§9's one mechanism, checked against the templates rather than asserted.

    This is the test the first draft of the motion layer needed and did not have. That
    draft listed swap targets by id, from memory, and missed three of them — the
    decisions ledger, the memory ledger and the week grid. Nothing failed: the page
    rendered, the write landed, and one fragment simply snapped into place while the
    fragment beside it faded. A stylesheet cannot report that, and neither can a test
    that only reads the stylesheet, which is why this one reads both.

    A region qualifies by id or by any class on its root element. Classes are the
    better answer — `.panel` and `.sec` are what a swappable region already is here, so
    a fragment added later inherits the animation — and the ids are for the handful
    that wear no such class.
    """
    # Every `@starting-style` block, not the first: an inline region takes a fade
    # without a translate — a transform does not apply to it, and making it a block to
    # earn one would move the text around it — so it carries its own rule.
    blocks = re.findall(r"@starting-style\s*\{\s*([^{]+)\{", _sheet())
    assert blocks, "the arrival rule is gone"
    selectors = {s.strip() for block in blocks for s in block.split(",") if s.strip()}

    unreached = []
    for target, (template, classes) in sorted(_swap_targets().items()):
        by_id = f"#{target}" in selectors
        by_class = any(f".{c}" in selectors for c in classes.split())
        if not (by_id or by_class):
            unreached.append(f"#{target} ({template}, class={classes!r})")

    assert not unreached, (
        "these regions are replaced by a write and reach no arrival rule, so they "
        "snap in beside fragments that fade:\n  " + "\n  ".join(unreached)
    )


def test_nothing_animates_on_the_way_out() -> None:
    """§9's first omission: only arrivals are free.

    Fading a panel out means holding the swap open while it fades — htmx's
    `defaultSwapDelay` is 0 for exactly that reason — and that is latency added in
    front of Resolve, Done and every checklist tick, which are the most frequent
    actions in the product. The swapping rule may set opacity; it may not spend time.
    """
    swapping = re.search(r"\.htmx-swapping\s*\{([^}]*)\}", _sheet())
    assert swapping, ".htmx-swapping is gone"
    assert "transition" not in swapping.group(1), (
        "a transition on the outgoing element is time added to every write on the "
        "site, and with defaultSwapDelay at 0 it does not even play"
    )


def test_the_theme_flip_cleans_up_after_itself() -> None:
    """The one universal selector in the stylesheet is affordable only because it is
    scoped to a class that exists for the length of one cross-fade.

    A `.theming` that is added and never removed leaves every element on the page
    carrying a colour transition for the rest of the session, which would make the
    0.55 in-flight dim and every hover fade at the wrong speed.
    """
    base = BASE.read_text()
    assert "classList.add('theming')" in base
    assert "classList.remove('theming')" in base, (
        "the theming class must be removed; a permanent one puts a colour transition "
        "on every element on the page"
    )
    universal = re.search(r":root\.theming\s*,\s*:root\.theming\s*\*", _sheet())
    assert universal, "the theme cross-fade rule is gone"


def test_the_acted_on_row_acknowledges_its_own_write() -> None:
    """§9's table asks how often the owner sees a thing. Resolve, Snooze and Drop are
    the most frequent writes in the product, and until 2026-08-11 the only thing that
    moved between the click and the replaced panel was one control dimming — on a card
    the width of the grid. The card the write belongs to now dims and lifts with it.

    Asserted against the sheet for the same reason every test in this file is: a
    transition is not in the DOM and not in the response body.
    """
    sheet = _sheet()
    rule = re.search(r"\.card:has\(\.btn\.htmx-request\)\s*\{([^}]*)\}", sheet)
    assert rule, "the acted-on card no longer acknowledges its write"
    body = rule.group(1)
    assert "opacity" in body and "transform" in body, "§9 rule 1: these two only"
    assert "--motion-travel" in body, (
        "§9 rule 3: the distance is the system's one distance, and routing it through "
        "the token is also what zeroes it under prefers-reduced-motion"
    )


def test_the_in_flight_dim_does_not_compound() -> None:
    """Two nested dims multiply, and the control the owner is looking at ends up the
    faintest thing on the screen — .55 x .55 is .30, below the sheet's muted ink."""
    assert re.search(
        r"\.card:has\(\.btn\.htmx-request\)\s+\.htmx-request\s*\{[^}]*opacity:\s*1", _sheet()
    ), "the button's own dim must be cancelled inside a dimming card"


def test_the_confirmation_script_is_loaded_wherever_a_write_can_happen() -> None:
    """Every page can write, which is why the failed-write strip is in base.html. Drop's
    confirmation is the same kind of thing and belongs in the same place."""
    assert "/static/confirm.js" in BASE.read_text()
