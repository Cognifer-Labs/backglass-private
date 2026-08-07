# Tiles — every content unit its own bounded surface

**Goal (owner, 2026-08-06, after the rounding pass):** "all content should be seperated in
some way, each having individual tiles, highlighted a different colors or outlined"

## What this reverses, stated once

Three things, all the owner's own, all re-opened by this instruction:

1. **The board register ruling (2026-08-01)** — "the boxed card stack outgrew the panel. A
   commitment is now one ledger line." Rows became hairline-separated ledger lines
   precisely to stop being boxes.
2. **The urgency-register ruling (2026-08-05)** — per-row washes were removed because
   "eight overdue rows made a wall of vermilion boxes"; state moved into the section
   heading instead.
3. **§8 rule 1** — "Color means state. Nothing decorative gets an ink."

The owner has asked for boxes and colour back. Fine — but the failure those rulings
recorded is real and a naive implementation walks straight back into it, so the design has
to answer it rather than ignore it.

## The constraint that decides the design

`--*-wash` tokens resolve to the **full saturated inks in dark mode** (tokens.css:130-135;
this is deliberate — it reproduces the pre-wash rendering with no per-component theme
fork). So "every tile gets a coloured fill" renders in dark as a page of solid vermilion /
gold / green / turquoise / cobalt blocks. That is the 2026-08-05 wall, worse.

The owner's own wording gives the way out: *"highlighted a different colors **or**
outlined."*

**So: the colour rides on the keyline, and the surface stays neutral in both themes.**
Every tile is outlined; the outline's ink says what kind of content the tile holds. Full
washes stay reserved for genuine state (overdue, awaiting, going cold), exactly as now, so
a state tile still outranks its category tile and is still legible against it.

## The design

**One tile treatment**, generalised from `#panel-awaiting .row`, which already does this
and is the proof it works:

```
surface   var(--fill-mild)        — neutral, both themes
keyline   1px, category ink       — the "different colour"
radius    var(--radius-3)         — from the rounding scale
padding   var(--sp-3)
gap       var(--sp-2) between tiles
```

The hairline rules between rows go away — separation is now the gap, which is what
"separated in some way" asks for. Section seams (2px) stay: they divide *panels*, and the
tiles divide *content*.

**Category inks**, one per kind of content, assigned by meaning rather than by decoration —
this is the amendment to §8 rule 1, and it is a category, not an ornament:

| content | ink |
|---|---|
| commitments | ink (neutral-strong) |
| schedule / plans | cobalt |
| goals + checklist | green |
| people / sources | turquoise |
| roadmap steps | gold |
| memory / decisions | ink-2 |

**State still wins.** `.src.cold`, `#panel-awaiting .row`, `.rmrow.overdue` and friends keep
their wash + saturated line, declared later in the sheet, so an overdue tile still reads as
overdue rather than as its category.

## Steps

- [ ] 1. `.tile` base in dashboard.css + a `--tile-line` custom property so a panel sets its
      category ink once instead of every row restating it.
- [ ] 2. Apply across every content surface: `.row`, `.card`, `.src`, `.chk`, `.goal`,
      `.sgoal`, `.tt li`, `.rmrow`, `.goalrow`, `.tot`, `.atl .stepx`, memory + decisions
      rows. Remove the hairline `border-top` idiom those carry today.
- [ ] 3. Kill the `:first-of-type{border-top:0}` resets that exist only to serve hairlines,
      and the negative-margin refunds on the washed rows (a tile no longer needs to bleed
      into the gutter — every row is inset now).
- [ ] 4. Check every fold/summary that wraps rows (`.wkfold`, `.goalrow`, `.atl summary`,
      `.more`, `.edfold`) still reads once its children are tiles.
- [ ] 5. `design/preview.html` mirrors it.
- [ ] 6. Docs: a new design-system section for the tile register + the §8 rule 1 amendment,
      and the CLAUDE.md decisions row.
- [ ] 7. Tests: the panel-slice tests couple to markup — grep for structural couplings
      first (CLAUDE.md testing expectations say to do this *before* restructuring shared
      markup). Add a test that every content surface carries a tile keyline.
- [ ] 8. Screenshot every page in both themes. This is a change whose correctness is almost
      entirely optical: density, and whether six inks on one screen reads as a system or as
      confetti. Expect to tune.
- [ ] 9. Workflow verification at the end.

## Definition of done

1. `uv run pytest` green.
2. Every content unit on every page is a bounded tile with a category keyline.
3. Dark mode checked on every page — no wall of saturated blocks.
4. State tiles still outrank category tiles and still read as state.
5. Docs no longer state a rule the CSS breaks.

## Carried over, not done

- `design/tokens.json` paper `#FAF3DF` vs `#FCF8EC` — flagged for the owner, untouched.
- Desktop `.app` bundles its own CSS copy; needs a rebuild to show any of this.
- Merge to main is the owner's step (worktree branch, shared checkout not mine to merge on).
