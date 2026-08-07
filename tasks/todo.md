# Rounding + tiles — shipped to main

**Goals, in the order they were given (2026-08-06 → 07):**

1. "go through the app and implement principle of rounding to all elements … use a workflow
   at the end for verification"
2. "add more rounding and screenshot to find any ui problems"
3. "all content should be separated in some way, each having individual tiles, highlighted a
   different colors or outlined"
4. "every item separated by MORE than a thin line — by color, or tile, or dark outline.
   Choose and apply throughout"
5. "update real app and check and improve rounding"

## State

**Main is at `1989626`.** The branch fast-forwarded onto it — no force, no rewrite, nobody's
working tree touched. 1616 tests passing.

## What shipped

**The radius scale (§7).** `--radius-1` 2px marks, `--radius-2` 4px controls, `--radius-3`
6px containers. Nesting is concentric, and the inset is padding **plus border**, because a
radius is measured on the border box. An element pinned on some edges rounds only the free
ones — the failed-write strip's top, the NOW tab's left, a chart column's top.

**The tile register (§7b).** Every content unit is a bounded tile: `--fill-mild` surface, 2px
keyline, `--radius-3`, separated by a gap instead of a hairline. The colour rides on the
**keyline**, never the fill — `--*-wash` resolves to the full saturated ink in dark, so a
coloured fill on every tile is a wall of blocks. Use the **raw inks** for a tile keyline,
never `--*-line`: those resolve to `var(--rule)` in dark and collapse every category into one
paper outline. Four category inks; gold and vermilion stay out so state keeps its alarms.

Specificity: **state beats kind beats category.**

## Verified

Two adversarial workflow passes (22 + 25 agents) over the rounding work; every confirmed
finding is fixed. The tile work was verified by eye against the real database in both themes,
plus the suite.

The catches worth remembering, all of them things only looking could find:
- A stray `}` had been deleting the base `.panel` rule since commit 8ea8ba9 — no panel seams,
  no closing padding, the `min-width:0` overflow fix inert. Hidden because the file also
  never closed its last `@media`, so the brace count balanced at 514/514.
- The reel's concentric example omitted its own border (4px where 6px was right).
- Category inks collapsed to a single colour in dark while looking correct in light.
- Goal-card target rows sat flush against the card frame, colliding curves.
- A card's hover cue was invisible inside the new folds — same token as the fold itself.

## Open, and the owner's call

- **`design/tokens.json` declares paper `#FAF3DF`**; every other surface says `#FCF8EC`.
  Nothing in the repo reads that file, so the drift is silent and permanent. Correct the
  mirror or delete it.
- **The desktop app is rebuilt but NOT installed.** The bundle is at
  `desktop/src-tauri/target/release/bundle/macos/Backglass.app` (62MB), signed with the
  stable Apple Development identity, and its frozen `dashboard.css` is byte-identical to the
  worktree's. Copy it over `/Applications/Backglass.app` yourself — the build script
  deliberately does not install. Signing is stable now (`8d5376a`), so the Downloads
  permission prompt should be a one-time cost rather than per-rebuild.
- **A goal card with only milestones and no targets** was never rendered; the inset rule
  covers both cases but only one was seen.

## Notes for whoever is next

- `--*-line` tokens are for **filled** components — the fill carries the colour and the line
  is the rule colour. Anything relying on a coloured **line** must use the raw ink.
- `panel_slice` is anchored on `id="panel-…"`. The tile work was pure CSS, so nothing that
  couples a test to markup had to move.
- Two sessions shared this repo all day. Every integration was a rebase from a worktree; the
  shared checkout was never touched, and migration 19 was borrowed untracked to serve locally
  and removed before committing.
