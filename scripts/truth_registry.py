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

# ── surfaces ──────────────────────────────────────────────────────────────────
PAPER = Fact(
    name="paper surface",
    authority=Site(TOKENS, r"^\s*--paper:\s*(#[0-9A-Fa-f]{6});", "tokens.css --paper"),
    mirrors=(
        Site("design/tokens.json", r'"paper":\s*\{\s*"hex":\s*"(#[0-9A-Fa-f]{6})"'),
        Site("design/design-system.md", r"^\| paper \| `(#[0-9A-Fa-f]{6})`", "design-system.md §2"),
        Site("design/preview.html", r"^  --paper:(#[0-9A-Fa-f]{6});", "preview.html tokens"),
        Site("design/wordmark.svg", r'<rect x="0" y="0" width="640" height="210" fill="(#[0-9A-Fa-f]{6})"'),
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

MUTED = Fact(
    name="neutral 500 (muted text)",
    authority=Site(TOKENS, r"^\s*--ink-muted:\s*(#[0-9A-Fa-f]{6});", "tokens.css --ink-muted"),
    mirrors=(
        Site("backglass/brief/render.py", r'^INK_MUTED = "(#[0-9A-Fa-f]{6})"', "render.py INK_MUTED"),
    ),
    normalise="hex",
)

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

FACTS = [PAPER, *INKS, MUTED, RADIUS_1, RADIUS_2, RADIUS_3, BORDER]
