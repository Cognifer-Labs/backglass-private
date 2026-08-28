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


# ── the scripted layer (static/motion.js) ────────────────────────────────────
#
# Two things moved out of the stylesheet in 2026-08-21 because CSS cannot express them:
# a stagger, and a row that moves because another row left. Everything else stayed. The
# tests above read the sheet because that is where the rules live; these read the script
# for the same reason, and they exist because a scripted duration is the easiest way in
# the world to reintroduce the drift §9 spends five tokens preventing.

MOTION_JS = ROOT / "backglass" / "web" / "static" / "motion.js"
STATIC = ROOT / "backglass" / "web" / "static"


def _script() -> str:
    """The script with its comments removed, for the reason `_sheet` strips the sheet's:
    the block comments here quote token names and durations while explaining them."""
    source = MOTION_JS.read_text()
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", source, flags=re.M)


def test_the_script_invents_no_duration_distance_or_curve() -> None:
    """§9 rule 3, enforced one layer further out than it used to reach.

    A component that invents a speed has made a system decision alone, and a *script*
    that invents one is worse: it is invisible to every test in this file, to the
    stylesheet, and to anyone reading design/tokens.css to find out what this product
    does. So the script is allowed to read tokens and is allowed to do arithmetic on
    them, and is not allowed to write a number with a unit on it.
    """
    body = _script()
    literals = re.findall(r"\b\d+(?:\.\d+)?(?:ms|s|px)\b", body)
    assert not literals, (
        f"motion.js writes {literals} literally; every duration and distance is a token"
    )
    assert "cubic-bezier" not in body, (
        "the curve belongs to --ease-out; a second copy of it is a second curve the "
        "moment either one is edited"
    )
    for name in ("--motion", "--motion-travel", "--ease-out", "--ease-in-out"):
        assert name in body, f"{name} is no longer where motion.js gets its values"


def test_the_script_stands_down_for_reduced_motion() -> None:
    """The token layer zeroes travel and press, which is enough for anything that moves
    by a distance. A stagger and a FLIP have no distance token to zero — a FLIP's
    distance is however far the row happened to move — so they are gated on the query
    itself, and this is the assertion that they still are."""
    assert "prefers-reduced-motion" in _script(), (
        "motion.js no longer asks; a FLIP cannot be zeroed by a token the way a "
        "transition can"
    )


def test_only_transform_and_opacity_are_scripted() -> None:
    """§9 rule 1 does not stop applying because the animation moved into JavaScript.
    Anything else animated here would be a layout or paint property being tweened from
    the main thread, sixty times a second, on the page this file exists to unblock."""
    animated = set()
    for call in re.findall(r"\.animate\(\s*\{(.*?)\}\s*,", _script(), flags=re.S):
        animated.update(re.findall(r"(\w[\w-]*)\s*:", call))
    assert animated, "no animate() calls found — has the file moved?"
    assert animated <= {"opacity", "transform"}, (
        f"motion.js animates {sorted(animated - {'opacity', 'transform'})}; §9 rule 1"
    )


def test_the_motion_script_carries_no_engine() -> None:
    """The animation is `Element.animate`, and that is the point.

    This file first shipped on Motion's `mini` build — 12 KB whose whole job is to call
    the API used here directly. docs/10 §Web layer asks for no build step, no npm and no
    bundler, and a browser API satisfies that more completely than a vendored copy of a
    wrapper around it: there is no file to check in, no hash to keep in step with it,
    and nothing to upgrade.

    Asserted rather than remembered, because the next animation that wants a spring will
    reach for a package, and the argument for not having one is easy to lose.
    """
    body = _script()
    assert ".animate(" in body, "the animations are gone"
    assert "import" not in body, (
        "motion.js pulls in an engine again; the Web Animations API is already in the "
        "browser and §Web layer rules out the build step a package implies"
    )
    assert not list(STATIC.glob("motion.min*")), (
        "a vendored animation engine is back in static/ — see this test's docstring"
    )


def test_the_motion_script_is_loaded_wherever_a_write_can_happen() -> None:
    """Same rule as the confirmation script above: every page can write, and the FLIP is
    what a write looks like. Deferred, so it never blocks the parse it decorates."""
    base = BASE.read_text()
    assert re.search(r'<script src="/static/motion\.js"\s+defer></script>', base)


def test_the_script_does_not_stagger() -> None:
    """§9: "No stagger anywhere. Panels arrive together."

    This test exists because the rule was broken. A stagger was added to the swapped-in
    rows on 2026-08-21, along with a sixth duration token to space it — in a system whose
    stated argument for having five is that you pay per duration. Both were reverted the
    same day on re-reading §9, which had refused the idea in advance and given the
    reason: the last item ends up a third of a second behind the first, on writes the
    owner fires tens of times a day.

    Asserted against `delay`, because that is the only way to express a stagger with the
    Web Animations API, and against the token, because reintroducing one would need it
    back.
    """
    body = _script()
    assert "delay" not in body, (
        "motion.js delays an animation; §9 refuses a stagger and every animation here "
        "belongs to the same beat"
    )
    assert "--motion-stagger" not in body, "the sixth duration token is back; §9 has five"
    assert "--motion-stagger" not in TOKENS.read_text(), (
        "tokens.css defines a stagger duration §9 forbids spending"
    )


def test_each_curve_is_used_for_what_it_was_defined_for() -> None:
    """§9 assigns the two curves by role: `--ease-out` for anything entering,
    `--ease-in-out` for anything moving on screen.

    The FLIP is the first thing in this product that moves on screen rather than
    arriving, and it went in on `--ease-out` because that was the curve everything else
    used. The distinction is the whole reason there are two.
    """
    source = MOTION_JS.read_text()
    flip = re.search(r"── 1\.(.*?)── 2\.", source, flags=re.S)
    fold = re.search(r"── 2\.(.*)", source, flags=re.S)
    assert flip and fold, "the two sections are no longer where this test looks"

    def curve(section: str) -> str | None:
        found = re.findall(r'ease\("(--ease-[\w-]+)"\)', section)
        return found[0] if found else None

    assert curve(flip.group(1)) == "--ease-in-out", (
        "the FLIP moves a row that is already on screen; §9 holds --ease-in-out for "
        "exactly that, and nothing else in the product had ever used it"
    )
    assert curve(fold.group(1)) == "--ease-out", (
        "an opening panel is content entering, and §9 gives everything that enters "
        "--ease-out"
    )


def test_the_fold_reveal_animates_opening_only() -> None:
    """§9's first omission: nothing leaves. A panel closing is a disappearance, and
    animating it would mean holding the fold open while it faded."""
    body = _script()
    assert ".open" in body, "the fold reveal no longer checks which way the panel went"
    assert re.search(r"!\w+\.open", body), (
        "the fold reveal must return early when the panel is closing, not animate it out"
    )


def test_the_flip_measures_the_live_document() -> None:
    """The bug that logged perfectly and drew nothing.

    For an `outerHTML` swap — which is what every write on this dashboard uses — htmx
    reports `event.detail.target` as the element it replaced, and that element is
    already detached when `htmx:afterSwap` runs. `getBoundingClientRect` on a detached
    node returns zeros, so the FLIP's delta came out as each row's absolute offset
    rather than the distance it moved, and the animation was handed to a node that
    would never be painted again. Nothing threw and every log line looked right.

    The `before` map is keyed by commitment id, so the live document is both the
    correct and the simplest thing to ask.
    """
    body = _script()
    # The loop moved into `travel()` on 2026-08-27 when mechanism 3 needed the same
    # second half; the property is about where it reads from, not where it lives, so it
    # is asserted against that function and against the handler that calls it.
    flip = re.search(r"function travel\(.*?\n\}", body, flags=re.S)
    assert flip, "the FLIP's second half is no longer recognisable"
    assert "document.querySelectorAll" in flip.group(0), (
        "the FLIP must read the live document after a swap"
    )
    handler = re.search(r'"htmx:afterSwap", \(\) => \{\s*travel\(.*?\}\);', body, flags=re.S)
    assert handler, "nothing spends the measurement after a swap any more"
    assert "detail" not in flip.group(0) + handler.group(0), (
        "event.detail.target is the element htmx replaced, and it is detached by now — "
        "measuring it gives a rect of zeros and animates a node that will never paint"
    )


# ── §9 mechanism 8: the row the owner removed ────────────────────────────────
#
# Added 2026-08-27 on the owner's ruling. The mechanism is one exit in a system that
# refused every exit until then, so what these hold is the boundary, not the animation:
# it may run before the request and it may not make htmx wait, the row may only be
# hidden and never deleted, and a refused write must put it back. Get any of those wrong
# and the surface is lying about what the ledger holds, which is CLAUDE.md rule 1 with
# the failure moved from a sentence to a row.

TEMPLATES = ROOT / "backglass" / "web" / "templates"


def _vanishing() -> list[tuple[Path, str, str]]:
    """Every control marked `data-vanish`, as (template, selector, the element's tag)."""
    found: list[tuple[Path, str, str]] = []
    for path in sorted(TEMPLATES.glob("*.html")):
        text = path.read_text()
        for match in re.finditer(r"<(button|a)\b[^>]*?data-vanish=\"([^\"]+)\"[^>]*>", text, re.S):
            found.append((path, match.group(2), match.group(0)))
    return found


def test_the_exit_runs_before_the_request_not_during_the_swap() -> None:
    """§9's amended first omission. The ban on exits was always a ban on *latency*: an
    exit inside the swap makes htmx wait, and `defaultSwapDelay` is 0 so it does not even
    play. Firing on `htmx:beforeRequest` is what makes this one free, and it is the whole
    justification for the amendment — so it is the thing asserted."""
    body = _script()
    assert 'addEventListener("htmx:beforeRequest"' in body, (
        "the exit no longer runs before the request; anywhere later is latency in front "
        "of the most frequent writes in the product"
    )
    swapping = re.search(r"\.htmx-swapping\s*\{([^}]*)\}", _sheet())
    assert swapping and "transition" not in swapping.group(1), (
        "the amendment permits an exit off the swap's critical path and nothing on it"
    )


def test_the_removed_row_is_hidden_and_never_deleted() -> None:
    """An optimistic view, never an optimistic record. The row has to survive so a
    refused write can put it back; a `remove()` here would make the rollback a re-render
    the client cannot do."""
    body = _script()
    assert ".remove()" not in body, (
        "a vanished row is hidden, not deleted — the rollback needs it to still exist"
    )
    assert "hidden = false" in body, "nothing puts a vanished row back"


def test_a_refused_write_returns_the_row() -> None:
    """The one unforgivable version of this feature is a row that vanished on a write
    that failed: the ledger still holds it and the screen says it does not. `oops.js`
    already says why; this is the half that puts the row back."""
    body = _script()
    for event in ("htmx:responseError", "htmx:sendError"):
        assert event in body, f"{event} no longer restores the row"
    # And it must be *that* request's row. Two exits are in flight whenever the queue is
    # worked in streaks, and a write that lands during the half-hourly sync waits on the
    # lock (the 2026-08-11 lesson), so "the last row that left" is the wrong answer often
    # enough to matter: it resurrects a row that is gone and leaves one that is not.
    restore = re.search(r"function restore\(.*?\n\}", body, flags=re.S)
    assert restore, "the rollback is no longer recognisable"
    assert "detail" in restore.group(0), (
        "the restored row must come from the failing request's own element"
    )


def test_the_hidden_attribute_actually_hides() -> None:
    """The attribute's `display:none` is in the UA stylesheet, so any author `display`
    beats it — and `.row` and `.card` both set one. Without an author rule of its own,
    mechanism 8 left the row holding its space at zero opacity, the gap never closed, and
    under reduced motion, where the fade is skipped on purpose, the click did nothing
    visible at all."""
    assert re.search(r"(^|\})\s*\[hidden\]\s*\{[^}]*display:\s*none\s*!important", _sheet()), (
        "a bare [hidden] rule is what makes mechanism 8's row leave; without it the "
        "attribute is advisory in this sheet"
    )


def test_every_vanishing_control_names_a_row_that_exists() -> None:
    """`data-vanish` carries the selector of the thing to remove, and a selector that
    matches nothing fails silently — the click would look exactly like the unfixed bug
    it was added for."""
    seen: dict[Path, set[str]] = {}
    for path, selector, _element in _vanishing():
        if path not in seen:
            seen[path] = {
                name
                for value in re.findall(r'class="([^"]*)"', path.read_text())
                for name in value.split()
            }
        wanted = {part.strip().lstrip(".") for part in selector.split(",")}
        assert wanted & seen[path], (
            f"{path.name}: data-vanish=\"{selector}\" matches no class in the template"
        )


def test_nothing_vanishes_that_does_not_leave() -> None:
    """Snooze is the case this rule exists for. It moves a commitment to tomorrow and the
    card stays on the board, so vanishing it would show the owner a row leaving and then
    a swap putting it straight back — a flicker that reads as a bug in the write."""
    for _path, _selector, element in _vanishing():
        assert "/snooze/" not in element, "Snooze does not remove the card; it moves it"
        assert "/untick" not in element and "/tick" not in element, (
            "a checklist tick toggles in place"
        )
        assert "/outcome/" not in element, (
            "a block marked done stays on the day with its outcome shown"
        )
