# Spacing between tiles, and layout inside them

**Goal (owner, 2026-08-07):** "look through the app to improve spacing between tiles and
improve layout within tiles to make sure everything within a tile looks well organised and
not squished. Run a workflow to do the audit."

Two things, and only two: the gap **between** sibling content units, and the arrangement of
content **inside** one. Colour, typography, radius and information architecture are out of
scope for this pass — the radius scale was settled on 2026-08-07 and the tile ruling on
2026-08-06, and re-opening either here would be relitigating a decision, not doing the work.

## How the evidence was gathered, and why it took three tries

The audit is driven from screenshots of the **real ledger** — 52 open commitments, 163
people, four roadmaps, a going-cold person, an overdue roadmap — because an empty database
renders empty tiles, and empty tiles have no layout to audit. Everything below is a trap
this pass actually fell into, recorded so the next visual audit does not pay for them again.

1. **The worktree's own database is near-empty.** The live one is 43M at
   `data/backglass.db` in the main checkout. It was copied to the scratchpad and served
   read-only from there; the live ledger was never pointed at a dashboard that can write.
2. **The live database is one migration ahead of this branch.** It records migration 19
   (`model_call`), which does not exist here, so `migrate()` refuses it. The scratchpad copy
   was downgraded — drop the table, delete the `schema_version` row — rather than importing
   main's migration into a branch that is not about that.
3. **A CSS edit needs a NEW PORT, not a reload.** The 2026-08-07 lesson: the HTTP cache is
   keyed by origin, so a reload after a stylesheet edit can serve stale bytes and a
   screenshot of stale bytes proves nothing. Each server here ran on a fresh port and the
   served stylesheet was `curl`ed and matched against disk before any frame was believed.
4. **Captures lie in three separate ways, and each needs its own check.** Five frames came
   back blank (the window was lost); four came back in the wrong theme (the toggle silently
   reverted between runs); one was a different page entirely (the navigation had not landed
   when the shutter fired). `scratchpad/checktheme.py` now checks theme and blankness across
   every frame before any of them is handed to an auditor — a blank screenshot produces a
   confident "no findings", which is indistinguishable from a page that is genuinely fine.
   Page identity is still eyeballed; the checker cannot see it.
5. **`getComputedStyle` is unreachable.** The dashboard's CSP allows `unsafe-inline` but not
   `unsafe-eval`, and the browser automation evaluates strings, so the DOM cannot be asked
   what it computed. `scratchpad/measure.py` reads the geometry off the rendered frame
   instead: it scans a line across a capture and prints every run of background and
   non-background in **CSS pixels**, which is how a declared value gets proven in rendered
   output rather than in a stylesheet that may not be the one being painted.

## The audit

A workflow (`tile-spacing-audit`, run `wf_3911df50-333`): one read-only auditor per page —
dashboard, schedule, week, goals, people, roadmaps, memory, chats, decisions, brief — each
given both themes of its own page plus the templates and the shared stylesheet; then one
cross-cutting pass over `dashboard.css` as a whole, because inconsistency *between* pages is
the one defect no single-page auditor can see; then a synthesis step that re-opens the files
to confirm every cited line before keeping a finding, merges duplicates, and resolves the
case where two pages want different values from the same shared rule.

Constraints handed to every auditor: proposed values land on the `--sp` scale, the
"what stays square" list is untouchable, no shadows or gradients, `:is()` arguments stay
single-class (it takes its most specific argument's specificity, which is how the state
washes were silently lost once already), and a padding change that alters an inset flags the
concentric-radius arithmetic around it.

## Steps

- [x] 1. Serve the real ledger, cold, with the stylesheet byte-matched against disk.
- [x] 2. Capture every page in both themes; verify theme, blankness and page identity.
- [x] 3. Fix the stale radius figures in the `dashboard.css` header — they still said 2/4/6
      after the scale moved to 3/6/10, and every auditor reads that header first. `truth.py`
      does not catch it: the registry covers values, and this was prose.
- [x] 4. Run the audit workflow. Twelve agents, 45 raw findings, synthesised to 33 after the
      synthesis step re-opened every cited line and dropped what it could not confirm.
- [x] 5. Reconcile against a first-hand read. All five were answered:
      - the roadmap metadata run-on was real and had two causes, not one — an 8px title-to
        -meta gap where the register says 4px, and one separator written as a literal `·`
        inside the flex text run where every other is a `.sep` span, so the same mark
        rendered at two widths on one line;
      - the commitment card's inverted proximity was real: only `column-gap` was overridden,
        so the wrap kept `.row`'s 16px — more than the 12px between two different cards;
      - the person row's badge crowding was NOT a badge problem. The washed rows padded 12px
        against the base tile's 16px, so a state row's whole text column stepped outward;
      - the CLOSED em-dash was not raised by any auditor and is **not fixed** — see below;
      - **ON TRACK rows are tiles.** I misread the frame. The real defect was the opposite of
        what I guessed: the tile's 16px stacked on the summary's pre-existing 12px, wrapping
        one 23px line in 28px of inset.
- [x] 6. Applied in one pass, one writer, `dashboard.css` backed up to the scratchpad first.
- [x] 7. Verified: fresh port 8942, stylesheet byte-matched against disk, both themes
      re-captured and checked, `measure.py` before/after.

## What the audit found that no one was looking for

The two highest-severity findings were not spacing bugs at all — they were the reason the
spacing could not be trusted. Both are written up in `tasks/lessons.md`:

1. **Twelve `border-top:0` resets survived the tile pass**, and the rule written to undo
   them set only a width. `border-top:0` also sets `border-top-style:none`, and a used
   border width is forced to zero while the style is none — so the first tile of every list
   on every page rendered with no top edge, in both themes, with the suite green.
2. **The grouped tile `:is()` was (0,1,1), not (0,1,0)**, because `.tt li` and `.mlist li`
   are a class plus an element. The state washes were never at risk, so nothing visible
   broke — but `.sgoal`'s tighter sidebar padding, written afterwards with a comment
   explaining why the 236px column needs it, had never once rendered. The test guarding this
   carried an explicit exemption for those two selectors; that exemption was the hole.

Proof in rendered pixels, roadmaps page, tile heights and gaps in CSS px:

| | tile 1 | gap | tile 2 | gap | tile 3 (washed) | gap | tile 4 |
|---|---|---|---|---|---|---|---|
| before | 87 | 12 | 89 | 12 | 82 | 8 | 89 |
| after | 85 | 12 | 85 | 12 | 86 | 12 | 85 |

Tile 1 was 2px short (the missing top edge), the washed tile 7px short (12px padding) and
glued to its neighbour at 8px. After: one rhythm.

## Found and deliberately not fixed

- **Overlapping week blocks touch.** A lane-1 block starts at exactly 50% and its lane-0
  partner has no right stop, so two overlapping events meet with no paper between them and
  the buried one reads as ending at midday. The fix needs the server to mark which lane-0
  entries actually have a partner — `_place()` knows, the template does not — and that is a
  data change, not a spacing one. Left for whoever owns the schedule next.
- **Washed tiles keep a 1px keyline** where every other tile carries 2px, and three legacy
  hairline tops survive on `.atl .stepx`, `.gcard .gtgt` and `.gcard .mlist li`. Border
  WEIGHT is the keyline pass's business; this pass drew the line at a MISSING edge being a
  separation failure and a wrong-weight edge not being one.
- **`.src.fail` pads `calc(var(--sp-3) - 2px)`**, leaving a failing source's text 13px from
  the tile edge against 18px for its siblings. Real, but no frame in the set shows a failing
  source, so it fails the evidence standard rather than being fixed blind.
- **The roadmaps CLOSED section** leaves an em-dash floating between two headings. It is an
  empty-state rendering, not a tile, and belongs to a copy pass.
- **`brief/render.py` uses off-scale spacing** (18px, 10px) in the email's inline styles.
  Email cannot resolve custom properties, so the copy is deliberate; but it is an
  unregistered mirror of nothing, and `truth.py` covers values, not spacing.

## Definition of done

1. `uv run pytest` green (1642), `node scripts/validate-palette.mjs` green.
2. `uv run python scripts/truth.py` reports zero STALE and zero ASK.
3. Every changed gap or padding proven in rendered pixels by `measure.py`, before and after.
4. Both themes re-captured and checked; the state washes still win their cascade — verified
   on the vermilion going-cold row in dark, which is where the last cascade break surfaced.
5. Anything found but deliberately not fixed is written down above with the reason.
