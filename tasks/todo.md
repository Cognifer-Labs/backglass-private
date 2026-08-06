# Rounding pass — the concentric-radius principle across every surface

**Goal (owner, 2026-08-06):** "go through the app and implement principle of rounding to
all elements. plan this accordingly and use a workflow at the end for verification."

**Reading of "principle of rounding":** the concentric-radius principle — every boxed
element carries a radius, and where one box sits flush inside another, the inner radius
equals the outer radius minus the inset, so the curves stay concentric instead of running
two unrelated arcs against each other.

## The one thing to state out loud

This reverses a locked decision. `design-system.md` §7 says "Radius 0 everywhere. 2px on
reel digit windows only", §8 rule 7 says "No shadows, no rounded corners, no gradients",
and the CLAUDE.md decisions table carries the same row. It is the owner's own rule and the
owner has re-opened it. **The rule does not get silently contradicted by shipped CSS** —
the docs are amended in the same change, in the dated-ruling format the file already uses
for the 2026-07-31 wash ruling. Shadows and gradients stay banned; only rounding moves.

Working in worktree `.claude/worktrees/rounding-pass` (branch `worktree-rounding-pass`,
based on origin/main 247e37b) because a second session is live in the shared checkout —
four dirty files, mtimes three minutes before session start.

Baseline before any edit: `uv run pytest` → **1598 passed**.

## The scale

Three steps, anchored to the reel window's existing 2px so the one radius already in the
system becomes step 1 rather than an exception:

| token | value | carries |
|---|---|---|
| `--radius-1` | 2px | marks under ~20px, data fills, inline windows |
| `--radius-2` | 4px | controls and chips — buttons, inputs, chips, alerts, badges |
| `--radius-3` | 6px | containers — cards, grids, timelines, washed rows |

Small on purpose. The system's keyline is a 2px black rule; a large radius on a 2px black
border reads as a bubble and takes the printed-sign language with it. `--radius-reel`
stays as an alias of `--radius-1` so nothing that references it breaks. `--radius: 0`
becomes `--radius: var(--radius-2)` — the default control radius — since it is the token
name the sheet's `border-radius:0` declarations were standing in for.

## Exemptions — the list "all elements" is measured against

These stay square, each for a structural reason, and the verifier is given this list:

1. **Full-bleed black section bars** (`.banner`, brief `_section_bar`) — they run edge to
   edge of their panel; a radius would leave four cream nicks against the panel seam.
2. **Panel cells and page seams** (`.panel`, `.mast`, `.side`, `.sec`) — these are rules
   between sections, not boxes. Rounding a grid cell breaks the continuous seam the
   2026-08-01 row-locked ruling exists to keep.
3. **The fixed failed-write strip** (`.oops`) — pinned to the viewport's bottom edge.
4. **The wordmark** — `wordmark.svg` has its own spec that forbids alteration.
5. **Hairline rules, tracks' rules, borderless ledger inputs** (`border-bottom` only) —
   no box to round.
6. **Outlook**: the brief's chips take a literal `border-radius`; Word-engine Outlook
   ignores it and renders square. That is a graceful degrade, not a defect.

## Steps

- [ ] 1. `design/tokens.css` + `design/tokens.json` — add the three-step scale, keep
      `--radius-reel`, repoint `--radius`.
- [ ] 2. `backglass/web/static/dashboard.css` — apply across every boxed component.
      Concentric pairs handled with `overflow:hidden` on the container where the child is
      flush (`.gcard`/`.ghd`, `.sws`/`.sw`, `.wk7` cells, `.track`/`.fill`), and with
      `calc()` where a child is inset by a known padding (`.reel` housing 4px over 2px
      windows across 2px of padding — exactly concentric).
- [ ] 3. `design/preview.html` — the rendered reference `dashboard.css` is lifted from.
      It drifts the moment the sheet moves; mirror every change.
- [ ] 4. `backglass/brief/render.py` — the three `_chip` variants take 4px. Section bar
      exempt (rule 1).
- [ ] 5. Docs, in the same change: `design-system.md` §7 + §8 rule 7 (dated ruling),
      `docs/06-dashboard.md` line 70, `CLAUDE.md` decisions row.
- [ ] 6. Tests: rewrite `test_dashboard.py::test_no_shadows_no_radius_no_gradients_outside_the_hatch`
      so it enforces the *new* rule (shadows/gradients still banned; radii must come from
      the token scale, not raw px), and flip `test_brief.py`'s `border-radius` assertion.
- [ ] 7. Verification workflow (the owner's explicit ask) — see below.

## Verification workflow

A Workflow-tool run after implementation, small and adversarial:

1. **Static audit** — every bordered component carries a radius; no stray
   `border-radius:0` outside the exemption list; concentric math holds where a child is
   flush inside a parent.
2. **Optical check** — mcp-safari screenshots of dashboard / goals / schedule / person in
   **both** light and dark. The 2026-08-06 chart lesson: a visual change's correctness is
   partly optical and has to be looked at. Radii meeting 2px black keylines, and the
   clipped children inside `overflow:hidden` containers, are exactly the class of thing no
   assertion catches.
3. **Suite** — `uv run pytest` green, compared against the 1598-passed baseline.

## What the verification workflow found (25 agents, 4 lenses, adversarial refutation)

Every finding below was attacked by a fresh skeptic told to refute it; most were refuted.
These survived, and all are fixed except the last:

1. **HIGH, and not mine.** A stray top-level `}` at `dashboard.css:150` was swallowed into
   the next rule's selector prelude, making it `} .panel` — invalid — so the entire base
   `.panel` rule was silently dropped from the stylesheet. The dashboard had no panel
   bottom seam, no right seam, no closing padding, and the `min-width:0` grid-overflow fix
   (whose own comment documents a 390px viewport measuring 642px wide) was inert. Two
   agents proved it independently in WebKit with an A/B harness on the served bytes.
   Introduced by commit 8ea8ba9, an ancestor of this work. It survived because the file
   also never closes its final `@media(max-width:420px)` — the two errors cancel, so the
   brace count balances at 514/514 and no lint would ever flag it. Both fixed; the seam is
   visibly back.
2. **The reel's arithmetic — the ruling's own worked example — was wrong.** A radius is
   measured on the border box, so the window is inset by padding *and* border: 2+2+2 = 6px,
   not 4px. At 4px the plate's inner arc was 2px, identical to the window it was supposed
   to sit a step outside of, so the two curves ran flat instead of parallel. Fixed in CSS,
   preview.html, tokens.css and §7 — the principle is stated with the border included now.
3. **`.wk7`'s new `overflow:hidden` clipped the day-header focus ring.** The headers are
   links flush on the container's edge and the global ring is outset 2px. Turned inward.
4. **`.bar .b` rounded its zero end.** Same axis argument as the chart columns, and it now
   matches `.fill`, whose left end is already squared by the track's 0px inner corner.
5. **The exemption list was imprecise in three places** — `.row.k-*` over-claimed
   (`.row.sched.k-protected` is a full box and does round), the wordmark was missing from
   the CSS copy, and neither list named the band fills (nav row, hovered ledger row, today
   column, chart knockout). Rewritten with the membership test spelled out.
6. **`tasks/plan.md:321` still stated the retired rule** — the only surviving false copy of
   it in the repo. Amended.
7. **Three holes in my own new tests**, each proved by mutation: the token-scale test
   scanned only the `border-radius` shorthand (per-corner longhands sailed past), accepted
   any value merely *containing* `var(--radius`, and the square test matched only exact
   selector text, so a radius on `details.panel > summary.banner` would have passed. All
   three closed and re-proved red by mutation.
8. **NOT FIXED — flagged for the owner.** `design/tokens.json:4` declares paper as
   `#FAF3DF`; every other surface says `#FCF8EC`. Pre-existing, unrelated to rounding, and
   nothing in the repo reads `tokens.json` — so the drift is silent and permanent. It is a
   palette value, not a corner, and the right call (correct it, or delete a mirror file
   nothing consumes) is the owner's.

## Definition of done

1. `uv run pytest` green, ≥1598 passed.
2. Every boxed component in `dashboard.css` carries a token radius or appears on the
   exemption list above.
3. `design/preview.html` and `dashboard.css` agree.
4. `design-system.md`, `docs/06`, and `CLAUDE.md` no longer state a rule the CSS breaks.
5. Screenshots in both themes reviewed by eye, not just by assertion.

## Notes for after

The desktop `.app` bundles its own copy of `dashboard.css` under
`desktop/src-tauri/.../sidecar/`. It will show the old square corners until rebuilt, and a
rebuilt bundle is a new TCC principal (2026-08-06 lesson) — expect the one-time Downloads
permission prompt. Out of scope for this change; flagged, not done.
