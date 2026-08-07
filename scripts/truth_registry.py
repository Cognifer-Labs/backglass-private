"""The facts that have actually drifted, and where each one is written down.

Membership is earned, not enumerated. A fact belongs here once it has been written in two
places and the copies disagreed — seeding it with every value in the repo is how a tool like
this becomes shelfware nobody updates. Every entry below corresponds to a real incident:

  paper surface  tokens.json carried #FAF3DF for however long, and every published contrast
                 ratio in the repo had been computed against it.
  the five inks  same blast radius; they happened to survive, which is luck, not design.
  radius scale   tokens.json still said [2, 4, 6] a day after the rescale.
  chip radius    render.py pinned "4px" to a comment naming --radius-2, which moved to 6px.

`design/tokens.css` is the authority for all of them. It is the file the browser actually
loads, which is the only definition of "true" that survives an argument.

Deliberately NOT registered:

  tasks/lessons.md, tasks/todo.md   They record superseded values on purpose ("4px where 6px
                                    was right"). Registering them would have the tool correct
                                    its own history.
  the exemption lists               design-system.md vs the dashboard.css header state the
                                    same rules in prose. Regex over prose is a research
                                    project; the adversarial reviews already catch those.
  desktop/ bundle copies            Frozen at build time and stale until rebuilt, by design.
"""

from __future__ import annotations

from truth import Fact, Site

TOKENS = "design/tokens.css"

#: Files the orphan scan must not flag. Each is a deliberate exclusion, not an oversight —
#: an ignore list that grows without reasons is how a checker gets quietly disabled.
ORPHAN_IGNORE = (
    # They record superseded values on purpose ("#FAF3DF, an earlier paper"). Flagging them
    # would mean deleting the account of what went wrong.
    "tasks/*.md",
    # The checkers. They must name colours in order to assert against them; registering the
    # tests as mirrors would make them mirrors of the thing they check, which is the exact
    # circularity that let RADIUS_CHIP rot.
    "tests/*.py",
    # This registry describes where values live; it holds patterns, not values.
    "scripts/truth_registry.py",
    # Frozen at build time and stale until the app is rebuilt, by design.
    "desktop/*",
)

# ── surfaces ──────────────────────────────────────────────────────────────────
PAPER = Fact(
    name="paper surface",
    authority=Site(TOKENS, r"^\s*--paper:\s*(#[0-9A-Fa-f]{6});", "tokens.css --paper"),
    mirrors=(
        Site("design/tokens.json", r'"paper":\s*\{\s*"hex":\s*"(#[0-9A-Fa-f]{6})"'),
        Site("design/design-system.md", r"^\| paper \| `(#[0-9A-Fa-f]{6})`", "design-system.md §2"),
        Site("design/preview.html", r"^  --paper:(#[0-9A-Fa-f]{6});", "preview.html tokens"),
        # Every paper fill and stroke, not the first. The mark paints paper on its band, its
        # inner plate and its four reel windows; they are one fact restated, and checking
        # one of them would have let the other seven drift exactly as they already did once.
        # The negative lookahead is what carries the semantics a regex otherwise cannot: the
        # wordmark is two-colour, so "every fill that is not the ink" is precisely "every
        # paper fill", and a third colour appearing would surface here as a disagreement.
        Site("design/wordmark.svg", r'(?:fill|stroke)="(#(?!000000)[0-9A-Fa-f]{6})"',
             "wordmark.svg paper fills", occurrences="all"),
        # The reel keeps literal hexes on purpose — §7 makes it a physical object with a
        # black housing and paper wheels in BOTH themes, so it must not invert with the
        # tokens. Deliberate literals are still copies: if paper moves, the wheels stop
        # matching the page they sit on. Registered so they move with it.
        Site("backglass/web/static/dashboard.css", r"(?:background|border-color):(#FCF8EC|#[0-9A-Fa-f]{6})\b",
             "dashboard.css reel wheels", occurrences="all"),
        Site("backglass/brief/render.py", r'^PAPER = "(#[0-9A-Fa-f]{6})"', "render.py PAPER"),
        # The validator is the declared source of truth for every contrast figure in the
        # system, and it hardcodes the surface it measures against. That makes it a mirror
        # with authority's reputation — the worst kind to leave unwatched.
        Site("scripts/validate-palette.mjs", r"const PAPER = '(#[0-9A-Fa-f]{6})';"),
        Site("CLAUDE.md", r"\| Cream `(#[0-9A-Fa-f]{6})` and black", "CLAUDE.md decisions"),
    ),
    normalise="hex",
)

# ── the five inks ─────────────────────────────────────────────────────────────
def _ink(name: str, *, in_render: str | None = None) -> Fact:
    mirrors = [
        Site("design/tokens.json", rf'"{name}":\s*\{{\s*"hex":\s*"(#[0-9A-Fa-f]{{6}})"'),
        Site("design/design-system.md", rf"^\| {name} \| `(#[0-9A-Fa-f]{{6}})`", f"design-system.md §3 {name}"),
        Site("design/preview.html", rf"--{name}:(#[0-9A-Fa-f]{{6}});", f"preview.html --{name}"),
    ]
    if in_render:
        mirrors.append(
            Site("backglass/brief/render.py", rf'^{in_render} = "(#[0-9A-Fa-f]{{6}})"', f"render.py {in_render}")
        )
    if name == "vermilion":
        # A hex quoted inside a comment is still a copy, and this one proves it: the same
        # comment quoted the contrast as 4.3:1, which was the figure for a paper colour the
        # system had already left behind. Prose is out of scope for this tool, but a literal
        # embedded in prose is not prose — it is a value, and it rots like one.
        mirrors.append(
            Site("backglass/web/static/dashboard.css", r"colour: (#[0-9A-Fa-f]{6}) on cream",
                 "dashboard.css .lnk.danger note")
        )
    # The validator hardcodes all five. It is the declared source of truth for every
    # contrast figure in the system, which makes an unwatched copy inside it the most
    # expensive kind: it would go on reporting PASS against a colour nothing else uses.
    mirrors.append(
        Site("scripts/validate-palette.mjs", rf"^  {name}:\s*'(#[0-9A-Fa-f]{{6}})',",
             f"validate-palette.mjs {name}")
    )
    return Fact(
        name=f"{name} ink",
        authority=Site(TOKENS, rf"^\s*--{name}:\s*(#[0-9A-Fa-f]{{6}});", f"tokens.css --{name}"),
        mirrors=tuple(mirrors),
        normalise="hex",
    )


INKS = [
    _ink("vermilion", in_render="VERMILION"),
    _ink("gold", in_render="GOLD"),
    _ink("green"),
    _ink("turquoise", in_render="TURQUOISE"),
    _ink("cobalt"),
]

# ── the neutral ramp ──────────────────────────────────────────────────────────
# Registered as a family rather than one step, because the orphan scan is only as wide as
# the facts it knows: an unregistered step is a value nothing looks at, which is the state
# every drift in this repo started from. The ramp is restated in tokens.json and in §2's
# table, and the validator hardcodes the three it measures.
_VALIDATOR_NEUTRALS = {"700": "neutral 700 secondary", "500": "neutral 500 muted",
                       "200": "neutral 200 secondary", "300": "neutral 300 muted"}


#: The theme aliases, and the ramp step each resolves to. An alias is a copy of a ramp value
#: under a role name — `--ink-muted` is not a fact, it is neutral 500 wearing a job title —
#: so it belongs here as a mirror rather than as a fact of its own. Modelling it as a fact
#: made the two flag each other as unregistered copies, which is the registry telling you the
#: model is wrong. `nth` picks the theme: light blocks come first in both files, dark second.
_ALIASES: dict[str, list[tuple[str, int]]] = {
    "50": [("fill-mild", 0)],
    "100": [("fill-mild-2", 0)],
    "200": [("rule-hair", 0), ("ink-2", 1)],
    "300": [("ink-muted", 1)],
    "500": [("ink-muted", 0)],
    "700": [("ink-2", 0), ("rule-hair", 1)],
    "800": [("fill-mild-2", 1)],
    "900": [("fill-mild", 1)],
}


def _neutral(step: str, hex_lower: str) -> Fact:
    mirrors = [
        Site("design/tokens.json", rf'"{step}":\s*\{{\s*"hex":\s*"(#[0-9A-Fa-f]{{6}})"',
             f"tokens.json neutral {step}"),
        Site("design/design-system.md", rf"^\| {step} \| `(#[0-9A-Fa-f]{{6}})`",
             f"design-system.md ramp {step}"),
    ]
    for alias, nth in _ALIASES.get(step, []):
        theme = "light" if nth == 0 else "dark"
        # The alias in the authority's own file counts as a mirror. It is still a second
        # place the value is written, and "it lives next to the definition" has never
        # stopped a copy from drifting.
        mirrors.append(
            Site(TOKENS, rf"--{alias}:\s*(#[0-9A-Fa-f]{{6}});", f"tokens.css --{alias} ({theme})",
                 nth=nth)
        )
        mirrors.append(
            Site("design/preview.html", rf"--{alias}:(#[0-9A-Fa-f]{{6}});",
                 f"preview.html --{alias} ({theme})", nth=nth)
        )
    if step in _VALIDATOR_NEUTRALS:
        mirrors.append(
            Site("scripts/validate-palette.mjs",
                 rf"\['(#[0-9A-Fa-f]{{6}})',\s*\w+,\s*[\d.]+,\s*'{_VALIDATOR_NEUTRALS[step]}"),
        )
    if step == "200":
        # The brief draws its row hairline with a literal, because email cannot resolve a
        # custom property. Same class of copy as RADIUS_CHIP, same reason to watch it.
        mirrors.append(
            Site("backglass/brief/render.py", r"border-bottom:1px solid (#[0-9A-Fa-f]{6});",
                 "render.py row hairline")
        )
    if step == "500":
        mirrors.append(
            Site("backglass/brief/render.py", r'^INK_MUTED = "(#[0-9A-Fa-f]{6})"',
                 "render.py INK_MUTED")
        )
    return Fact(
        name=f"neutral {step}",
        authority=Site(TOKENS, rf"^\s*--n-{step}:\s*(#[0-9A-Fa-f]{{6}});", f"tokens.css --n-{step}"),
        mirrors=tuple(mirrors),
        normalise="hex",
    )


NEUTRALS = [
    _neutral(step, h)
    for step, h in (("50", "#f5eedb"), ("100", "#e4decd"), ("200", "#c9c4b5"),
                    ("300", "#aca79b"), ("400", "#8e8a81"), ("500", "#716f67"),
                    ("600", "#57554f"), ("700", "#3d3c37"), ("800", "#252421"),
                    ("900", "#0e0d0b"))
]

# ── geometry ──────────────────────────────────────────────────────────────────
RADIUS_1 = Fact(
    name="radius step 1 (marks)",
    authority=Site(TOKENS, r"^\s*--radius-1:\s*(\d+)px;", "tokens.css --radius-1"),
    mirrors=(
        Site("design/tokens.json", r'"radius":\s*\[(\d+),', "tokens.json geometry.radius[0]"),
        Site("design/tokens.json", r'"radiusReel":\s*(\d+)', "tokens.json geometry.radiusReel"),
        Site("design/preview.html", r"--radius-1:(\d+)px;", "preview.html --radius-1"),
    ),
    normalise="px",
)

RADIUS_2 = Fact(
    name="radius step 2 (controls)",
    authority=Site(TOKENS, r"^\s*--radius-2:\s*(\d+)px;", "tokens.css --radius-2"),
    mirrors=(
        Site("design/tokens.json", r'"radius":\s*\[\d+,\s*(\d+),', "tokens.json geometry.radius[1]"),
        Site("design/preview.html", r"--radius-2:(\d+)px;", "preview.html --radius-2"),
        # The one this tool was built to catch. A chip in the morning brief is the same
        # control as a chip on the dashboard; email cannot resolve a custom property, so the
        # value is copied — and a copy with a comment naming its source is still a copy.
        Site("backglass/brief/render.py", r'^RADIUS_CHIP = "(\d+)px"', "render.py RADIUS_CHIP"),
    ),
    normalise="px",
)

RADIUS_3 = Fact(
    name="radius step 3 (containers)",
    authority=Site(TOKENS, r"^\s*--radius-3:\s*(\d+)px;", "tokens.css --radius-3"),
    mirrors=(
        Site("design/tokens.json", r'"radius":\s*\[\d+,\s*\d+,\s*(\d+)\]', "tokens.json geometry.radius[2]"),
        Site("design/preview.html", r"--radius-3:(\d+)px;", "preview.html --radius-3"),
    ),
    normalise="px",
)

BORDER = Fact(
    name="border weight",
    authority=Site(TOKENS, r"^\s*--border:\s*(\d+)px;", "tokens.css --border"),
    mirrors=(Site("design/tokens.json", r'"border":\s*(\d+)', "tokens.json geometry.border"),),
    normalise="px",
)

FACTS = [PAPER, *INKS, *NEUTRALS, RADIUS_1, RADIUS_2, RADIUS_3, BORDER]
