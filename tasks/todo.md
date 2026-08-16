# Audit: what the telemetry says, and where the knowledge base actually sits

Written 2026-08-07, one day after Phase 0 landed. Thirty model calls is a small sample
and it is already enough to refute the premise the rest of the plan was resting on.

## What a call costs, measured

| tier | calls | median | p95 | mean chars | imputed per call |
|---|---|---|---|---|---|
| extract | 9 | 39.4s | 53.8s | 8,999 | **17.53c** |
| triage | 20 | 11.6s | 15.8s | 3,168 | 0.83c |
| triage_batch | 1 | 36.6s | — | 4,340 | 2.12c (~12 items) |

Three readings, and the first one changes the plan.

**Cost does not track payload, it tracks output.** A 13,372-char extraction cost 5.7c;
a 7,822-char one cost 22.5c. Duration tracks cost almost exactly (5.2s → 2.5c, 64s →
30c). What the model *writes* is the bill, not what it reads.

**So per-call session overhead does not dominate here.** If it did, cost would be
roughly flat across tiers; instead extract costs 21× triage. `CLI_ISOLATION_FLAGS` is
doing its job and the 2026-07-30 measurement — taken in a different context, on a
different call shape — does not describe this pipeline. **Phase 2.2 (batch extraction)
is withdrawn, not deferred**: batching saves per-call overhead, five items still emit
five items' worth of output, and crowding is how one item's evidence sentence gets
attributed to another. It was a plan built on an unmeasured premise, which is exactly
what Phase 0 existed to test.

**Batching triage is real, and nearly never happens.** 0.18c per item batched against
0.83c per item alone — 4.6× — but one batch call fired across thirteen runs, because
`triage_batch_min` is 4 and a run typically has one to three items pending. Worth
fixing and worth almost nothing: triage is 16.6c of the window's 176.6c.

**Everything is extraction.** A run with no extractions costs about 1c; run 213, with
six, cost 101.6c. At 17.5c a call and 539 historically barren extractions, roughly
**$94 of imputed spend has bought nothing** — and that is the number every remaining
item should be measured against.

## Done in this pass

- [x] Templates learn from the same evidence senders do. `template_verdicts.sql` now
      reports `settled` / `productive` / `unsettled` beside `dropped`, and the tier-0
      rule drops a shape once enough siblings have been ANSWERED — dropped by the model,
      or kept and proved empty by the expensive pass on broadcast-marked mail — with one
      productive or one unanswered sibling disqualifying it. Identical in shape to the
      2026-08-05 sender fix and for the identical reason: "one keep, ever, disqualifies"
      aimed the right instinct at the wrong signal, and marketing mail is kept precisely
      because it is written to look like a deadline.
      **Measured on the live ledger: 18 shapes, zero productive siblings, 98 completed
      extractions that produced nothing — about $17 imputed, already spent, and
      recurring.** Four tests, two mutations proven red.

## Still open, reordered by the measurement

- [ ] 1. **Promote.** `backglass noise suggest` — 142 senders, owner's call.
- [ ] 2. `triage_batch_min` 4 → 2, so batching fires on the runs that have anything to
      batch. Cheap, measured, marginal.
- [ ] 3. Bulk headers at ingest (`List-Unsubscribe` and siblings). The body grep is a
      proxy for a machine-intended marker that the connector currently discards.
- [ ] 4. Phase 3's quality items are unchanged: 32 commitments past due on arrival, six
      duplicate clusters, uncalibrated confidence.

---

# The knowledge base is a notebook beside the pipeline, not inside it

Audited 2026-08-07. `fact` is what CLAUDE.md calls the personal knowledge base.

## What is actually there

Twenty facts across eight subjects — identity, education, premed, housing, preferences,
people, family, orgtruth. All of them true and useful. And:

- **Zero have a `source_item_id`.** Every one was typed by hand. A pipeline that has
  read 8,778 items has contributed nothing to the owner's knowledge base.
- **Zero are superseded.** The supersession machinery has never run.
- **Three readers exist, and none is the pipeline**: `facts.py` (the CLI), the Memory
  page, and the source page's "what came of this item". Neither triage nor extraction
  reads a single fact.

So both directions are disconnected. The model that decides what matters knows nothing
about the person it is deciding for, and the record of that person learns nothing from
the 8,778 documents it has read.

## Why that is the expensive gap, not a cosmetic one

The KB already contains `education.college = ASU Tempe, Barrett Honors, incoming fall
2026`. The ledger contains 95 sender domains and 18 template shapes of *other*
universities' admissions marketing, which cost 436 triage calls and 98 extractions and
produced nothing. A triage pass that knew the college decision was made would drop that
class on sight, as a rule rather than as a per-sender promotion the owner has to
approve one at a time.

That is the argument for integration, and it is also the argument for being careful:
the same fact, wrong or stale, would silently suppress real mail. A KB that steers the
pipeline needs provenance and a review path before it needs volume.

## How it should be implemented and integrated

Read direction first — it is the one with measured value, and it can be built without
touching a prompt.

- [ ] **A. Facts reach the RULE layer.** Open, and deliberately not attempted. The
      facts are prose ("ASU Tempe, Barrett Honors, incoming fall 2026") and a rule needs
      a domain or an address. Turning one into the other is either a new structured fact
      shape — which is `learned_noise` with extra steps, a third channel for one truth,
      the exact duplication item D exists to prevent — or an inference, which is a model,
      which is B. Left alone until there is a shape that is neither. Original framing: A fact with a
      `rule` shape (a domain, an address, a template class) can feed `rules.classify`
      the way `learned_noise` already does. No prompt version bump, no re-extraction, no
      eval question — and the same suggest/promote gate, because a rule derived from a
      fact is still a rule that can silence a real correspondent.
- [x] **B. Facts reach the triage prompt as owner context.** Done 2026-08-07, and much
      cheaper than this plan assumed — which is why the assumption was checked before
      building. Triage is NOT versioned: `pending_triage` selects on a NULL verdict, so
      bumping `triage.md` changes what future items see and re-reads none of the 8,778
      already judged. The token objection is answered by the telemetry above: cost
      tracks output, not input, so a few hundred characters on a 3,168-character prompt
      is close to free at the 0.83c tier. `{{owner_context}}` carries the fact table
      into both triage prompts — empty on a factless ledger, deterministic, bounded at
      900 chars, and stated rather than instructed so triage.md's keep-bias is untouched.
      Whether the model uses it well is an eval question and belongs in `evals/`.
      **This went before A, not after.** The plan gated it on A proving the facts
      trustworthy, which had the risk backwards: A acts on a fact with no model in the
      loop, where a wrong fact silently suppresses mail; here a wrong fact is one more
      line of context a model weighs against everything else.
- [ ] **C. Extraction writes facts back, last.** `source_item_id` exists on `fact` and
      has never been used. A durable fact learned from mail is a fifth record type
      beside commitment, engagement, checkpoint and evidence, and it needs the same
      treatment: confidence, the review queue below threshold (rule 2), supersession
      rather than edits, and provenance on every row (rule 1). This is the largest of
      the three and the only one that changes the extraction schema.
- [ ] **D. Settle the duplication before any of it.** `identity.emails` and
      `identity.timezones` restate `OWNER_EMAILS` and `DEFAULT_TZ`/`ALT_TZ` from `.env`.
      Two sources for one truth is how they drift; decide which one is canonical and
      have the other read it.

## Note on this file

This section was written on 2026-08-07 and dropped by a concurrent session's merge
resolution the same day — restored from commit 0a86456. Two sessions editing one
append-only log is how that happens; the halves of a todo.md conflict are almost never
alternatives, they are both real.

## Deliberately not

A vector store or embeddings over the fact table. CLAUDE.md's one idea forbids it and
it would not help: twenty rows keyed by subject and key are a lookup, not a search
problem, and the queries are known in advance.

---

# One truth per fact, and a tool that enforces it

**Goal (owner, 2026-08-07):** "resolve everything and make sure everything has a state that
is the truth, and any conflictors will be removed if they are stale — but if a conflictor is
more recent than the state doc then the user will be asked."

## Why this exists

Four times in two days a fact was written down in more than one place and the copies
disagreed, silently, with a green suite:

- `tokens.json` declared paper `#FAF3DF` while `tokens.css` shipped `#FCF8EC` — and *every*
  published `onPaper` ratio in the repo had been computed against the old colour.
- `tasks/plan.md` still stated the retired "no rounded corners" rule.
- The `.oops` comment cited an exemption its own commit had retired.
- **Live right now:** `backglass/brief/render.py` sets `RADIUS_CHIP = "4px"` against a
  comment binding it to `--radius-2`, which the rescale moved to **6px**. So the morning
  brief's chips render 4px while the dashboard's render 6px. `test_brief.py` asserts
  `border-radius:{render.RADIUS_CHIP}` — the checker is keyed to the mirror, so the value
  and its test are wrong together and green together.

That last one is the whole thesis. **A checker keyed to a mirror cannot detect drift in that
mirror.** Agreement between two copies can be wrong in both at once; only a value recomputed
or re-read from its declared authority can be trusted.

## Two definitions, stated because the wording could be read literally

- **"Removed"** means *corrected to the authority's value*, not the file deleted. Taking it
  literally would delete the design-system's tables, which are the documentation.
- **"The user will be asked"** means the tool refuses to auto-fix, exits non-zero, and names
  the conflict. In an interactive session that is a question; in CI it is a failure.

## The mechanism

`scripts/truth.py` — a registry plus a checker.

Each **fact** declares one **authority** (`file` + extractor) and N **mirrors**. An extractor
returns `(value, line_number)`.

For each mirror:

| | |
|---|---|
| values agree | **OK** |
| differ, mirror's line is **older** than the authority's | **STALE** → `--fix` rewrites it |
| differ, mirror's line is **newer** | **ASK** → no auto-fix, non-zero exit |

Recency is **per line**, via `git blame -L n,n`, not file mtime — a file touched for an
unrelated reason must not read as "recent". An uncommitted mirror line counts as newest and
therefore ASKs, which is right: an uncommitted edit is fresh human intent.

**The escalation loop is the point.** If the user rules that a newer mirror is correct, the
fix is to update the *authority* and re-run — every other mirror then goes STALE and
auto-fixes. One decision propagates everywhere.

## Load-bearing constraint

**A failed extraction is a loud error, never a skip.** If a pattern stops matching because a
table was reformatted, the tool must fail rather than silently declare the fact clean. A
checker that is green while blind is worse than no checker — that is exactly how the
`onPaper` figures survived.

## Scope fences

- **Values, not prose.** Hexes, px scales, ratios extract cleanly. The exemption lists in
  `design-system.md` vs the `dashboard.css` header are prose; regex over prose is where this
  turns into a research project. Prose consistency stays with the adversarial workflow
  reviews, which have caught it twice already.
- **History is not a conflictor.** `tasks/lessons.md` and `tasks/todo.md` deliberately record
  superseded values ("4px where 6px was right"). Excluded by design, or the tool corrects its
  own history.
- **Build artifacts excluded** — the desktop bundle's frozen copies are stale until rebuilt,
  by design.
- **Seed with the burned classes only.** A fact earns registry membership by having drifted.
  Enumerating all truth in the repo up front is how this becomes shelfware.

## Steps

- [ ] 1. `scripts/truth.py`: registry, extractors, git line-recency, classify, `--fix`.
- [ ] 2. Seed the registry: paper, the five inks, neutral-500, the three radius steps, the
      border weight — across `tokens.css` (authority), `tokens.json`, `design-system.md`,
      `preview.html`, `wordmark.svg`, `render.py`, `validate-palette.mjs`, `CLAUDE.md`.
- [ ] 3. Run it on the clean tree. **It must find `RADIUS_CHIP` as STALE** — that is the
      tool's acceptance test, and it is why the fix ships *with* the tool rather than before.
- [ ] 4. `--fix` it, and fix `test_brief.py` to assert against the authority rather than the
      mirror it is supposed to be checking.
- [ ] 5. `tests/test_truth.py` so drift fails the suite like everything else.
- [ ] 6. Prove both directions by mutation: seed a stale mirror → STALE; commit a mirror
      change newer than the authority → ASK.

## Also registering, because it claims authority it does not have

`scripts/validate-palette.mjs:14` hardcodes `PAPER = '#FCF8EC'`. It is the declared source of
truth for every contrast figure, but it is itself an unregistered mirror of `tokens.css` — if
paper changes again, the validator will validate the wrong colour while claiming to be the
authority.

## Definition of done

1. `uv run pytest` green; `node scripts/validate-palette.mjs` green.
2. `uv run python scripts/truth.py` reports zero STALE and zero ASK on a clean tree.
3. `RADIUS_CHIP` agrees with `--radius-2`, and `test_brief.py` checks it against the
   authority.
4. Both classifications proved by mutation.

---

# Motion layer: animate every action and every page change

Written 2026-08-07. Goal: "add animations for all actions and tab changes throughout
the site."

**Assumption, stated because nothing in the markup settles it:** there are no literal
tab controls in this app. "Tab changes" means the sidebar page navigation (nine full
document loads) plus the 1–9 keyboard page switch in `base.html`. Panel-swapping
HTMX targets are "actions", not tabs. Planned on that reading; no question asked.

## What is there now

One line of motion in the whole product: `.htmx-swapping{opacity:0;transition:.08s}`
at `dashboard.css:324` — and it never plays, because htmx 2.0.4's
`defaultSwapDelay` is `0`, so the outgoing node is replaced before a transition can
run. `defaultSettleDelay` is `20`. `design-system.md` has no motion section at all.

## Shape of the work

1. **Motion tokens** in `design/tokens.css` — durations and the three strong curves,
   with one `prefers-reduced-motion: reduce` block that zeroes the durations at the
   token layer instead of scattering overrides.
2. **One motion layer** appended to `dashboard.css`, single author, no per-page forks.
   Transform and opacity only (§8 rule 7 — no shadows, no gradients — extends to
   motion: things fade and slide, they never levitate).
3. **Swap animation with zero template edits** — `htmx.config.defaultSwapDelay` in
   `base.html` plus CSS on `.htmx-swapping` / `.htmx-added` / `.htmx-settling` covers
   all 94 `hx-` attributes at once. Every template edit avoided is a `panel_slice`
   test not broken.
4. **Page transitions** — `@view-transition { navigation: auto }` as progressive
   enhancement, probed in both Safari and the Tauri WKWebView rather than assumed,
   over an unconditional body fade-in baseline.
5. **§9 Motion** in `design-system.md`, with the frequency gate written down — this
   repo records its rulings there, so the section is deliverable, not garnish.

## Frequency gate (Kowalski), applied to this product

- Board Resolve/Snooze/Done and the 1–9 page keys fire tens of times a day: minimal
  or no motion. A daily action that got slower is a regression, not a polish pass.
- Panel swaps, the review queue, the failed-write strip: occasional — standard.
- Nothing here is rare enough to earn delight.

## Verification

- `uv run pytest` green (no template edits expected; prove it).
- `node scripts/validate-palette.mjs` after touching tokens.
- Adversarial CSS audit: transform/opacity only, every duration under 300ms, no
  `ease-in`, no keyframes on frequently-triggered elements, reduced-motion reaching
  every rule.
- Look at it in Safari, cache-busted, both themes (2026-08-06 lesson: a screenshot is
  a cache, not an observation).

## Motion: what the verifier caught

A fresh-context verifier refuted the first version of this. Three swap targets reached
no arrival rule — `#decisions`, `#memory` (both `.sec` sections) and `#week-grid`, the
region a checklist tick repaints out of band — and `.lnk`, which is the control the
whole roadmap surface is built from, was in no rule at all. The selector list had been
written from an id inventory, and an id inventory is a list that is correct on the day
it is written.

Fixed by selecting on `.panel`/`.sec` instead, and by adding the test that compares
every `hx-target` and `hx-swap-oob` region in the templates against the arrival rule.
Both the fix and the test were proven against a mutation: removing `.sec` lists five
regions by name.

---

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

Commitment cards, same method: every card is exactly 12px shorter (89/112/112/133 →
79/100/100/121), which is one wrap times the 16px→4px row-gap, while the card-to-card gap
stays 12.0 throughout. Inside-tile 4px, between-tile 12px — the inversion is gone.

**Two changes are not covered by any of that**, because a static frame cannot show them:
the hover-revealed `.acts` row (its seam goes 24px → 12px as a declared side effect of the
card's row-gap) and the disclosed card inside an open `.goalrow`. Both were reasoned through
the cascade, neither was rendered. If either looks wrong in use, that is where to look.

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

## Round two — the horizontal half

The owner then asked to continue "between tiles and words". Read as inline horizontal
rhythm: gaps between chips, badges, separators, labels and their values. Letter-spacing was
ruled out as typography — the .04–.06em spread is per-size and deliberate — except where two
elements at the same size and role disagree, and none did.

A narrower workflow than round one: four auditors on disjoint surfaces (chips and badges,
metadata lines, sources and sidebar, schedule and forms) plus a synthesis pass. Six raw
findings against round one's forty-five, which is the honest measure of what round one fixed.
Zero were dropped at verification.

**The failing anki source in every round-two frame is synthetic.** Round one parked the
`.src.fail` finding because no screenshot in the corpus showed a failing source and it
refused to fix blind. So this round set one `credential` row to `auth_expired` in the
SCRATCH COPY of the ledger — never the live one — which is what made the finding measurable.
Anki did not break on 2026-08-07; the alert in those captures is the seed.

**The trap this round, stated because it nearly generated a page of churn:** a metadata line
of literal `·` characters in a plain text run is CORRECT and self-consistent. It is a defect
only when a line MIXES flex-gap-spaced elements with literal separators, because then the
same mark renders at two widths on one line. That was true of exactly one line in the app
(`.rmst`, fixed in round one) and false everywhere else. The synthesis re-checked the
container's `display` for every candidate itself rather than taking the auditors' word:
`.cap`, `.rmclosed`, `.src .ts`, `.b2`, `.conf` are all plain blocks. Zero separator findings
survived, correctly.

Applied, five changes:

- **`.src.fail` stepped a failing source's whole content column 5px left of every sibling.**
  Its `calc()` compensated against `--sp-3`, the padding a `.src` had *before* it became a
  tile. This is the one row the owner is meant to find fast, and the misalignment read as
  part of the alarm. Now `calc(var(--border) + var(--sp-4) - 3px)` — 15px of padding under a
  3px keyline, which is the 18px every healthy row sits on. Its dead twin at the old line 527
  went with it: same specificity, later rule, all three properties restated.
- **The person-row badge cluster did no grouping** — chip-to-chip and text-to-cluster were
  both 12px, four equal gaps, so a tag, an open count and a going-cold warning read as three
  more columns. `.src .chip + .chip{margin-left:calc(var(--sp-2) - var(--sp-3))}` nets 8px
  inside the cluster and leaves 12px to the text it qualifies.
- **`+1` sat 20px from its own cadence label**, defending against a five-button row the
  markup cannot produce: `.cadopts` is *inside* the closed `<details>`, after the `<summary>`.
  Deleted rather than retuned.
- **The roadmap steps bar** sat as far from the count it draws as from the separator dividing
  that field from the next. Bound to 4px, the within-tile line gap.
- **The `.over` badge declared two icon gaps for itself**, 4px in the week header and 5px in
  the day strip. Both now 5px — the stated optical exception, sized to a 10px glyph, not
  snapped to the scale.

Proven in pixels: the status square's inset from the tile edge, measured on the rendered
frame, is **18.0 CSS px on the failing row and 18.0 on every healthy one**. It was 13 against
18. The badge-cluster change is 4px and was confirmed by reading the frame plus the
arithmetic; a scan line through that row crosses letterforms and cannot resolve it.

Carried forward, deliberately: `schedule.html:26`'s whitespace-strip welds the gold overflow
badge onto the preceding word with no gap (real, but needs a markup change and no frame in
the corpus renders an overflowing day); and `.row.k-work` and friends produce a 15px content
edge on the same 3px-keyline pattern this round set to 18px, so the sheet now carries two
compensation values for one idiom — worth one measured look, not worth changing blind.

## Definition of done

1. `uv run pytest` green (1642), `node scripts/validate-palette.mjs` green.
2. `uv run python scripts/truth.py` reports zero STALE and zero ASK.
3. Every changed gap or padding proven in rendered pixels by `measure.py`, before and after.
4. Both themes re-captured and checked; the state washes still win their cascade — verified
   on the vermilion going-cold row in dark, which is where the last cascade break surfaced.
5. Anything found but deliberately not fixed is written down above with the reason.

## 2026-08-09 — Apple Calendar reset to ASU Fall 2026 schedule

Request: wipe Apple Calendar, replace with the real ASU schedule.

Schedule source verified against primary evidence, not assumed: advising email
"Fall 2026 Enrollment Check" (Abby Kerr, 2026-08-05) lists BIO 181, CHM 113, CIS 236,
HON 171, LSB 191, PSY 101 — exactly the six courses behind the ledger's 200
`calendar:asu` rows. `LIA 101` sitting on the Work calendar is NOT an enrolled course.

Done:
- [x] Backup — `data/calendar-backup-2026-08-09/`: raw `Calendar.sqlitedb*`,
      `events-full.json` (76 events, all fields, 2000–2050 window), and
      `all-events-backup.ics` (double-clickable restore).
- [x] Deleted all 76 non-recurring events across the six writable calendars.
- [x] Created all 200 class meetings on `dkesava2@asu.edu`, each stamped
      `backglass:<external_id>` in its description. Verified: 200/200 present,
      0 duplicates, 0 field mismatches vs the ledger, 0 events on the term breaks
      (Sep 7, Oct 12–13, Nov 26–27).

- [x] Removed the 26 recurring events that Calendar.app's scripting bridge would not
      touch (Work 9, Family 16, contactdharsan@gmail.com 1), via EventKit —
      `~/Downloads/CalFix.app`, source in the session scratchpad. Two purge passes plus a
      third that removed 0, so the operation is idempotent. Also removed the stray
      "ZZ Backglass Test" calendar left by a probe.

Remaining — two minutes by hand:
- [ ] Two past-dated events survive on Family: `Acura insurance need to Renew`
      (2024-01-18) and `car insurance need to Renew` (2026-07-19). EventKit's
      `remove(span: .futureEvents)` truncated each RRULE to `UNTIL == its own start`
      rather than deleting the series, orphaning one first occurrence that the
      occurrence-range predicate no longer returns — so the tool can no longer see what
      it half-deleted. Delete both in Calendar.app.
      A `nuke` mode that removes by `event(withIdentifier:)` is built and the two UUIDs
      are in `data/calfix-ids.txt`, but rebuilding the binary changed its ad-hoc
      signature, which invalidates the TCC grant; the re-prompt went unanswered across
      three launches. Not worth another round for two dead events.

Tool safety, if calfix is ever run again: purge excludes `dkesava2@asu.edu` outright and
skips any event whose notes carry a `backglass:` stamp. Two independent guards, because
the first version of it would have deleted the 200 events it was run to protect.

Follow-up — **resolved, no work needed.** The concern was that the next sync re-ingests
these 200 as `calendar:apple` beside the existing `calendar:asu` rows and the day gets
charged twice. Measured against live data rather than reasoned about: the connector
returns 18 events inside its 21-day horizon, the ledger holds the same 18 on those days,
and `capacity._distinct` collapses 18 + 18 → 18 with **zero** non-matching identities.
The two spellings (`2026-08-20T10:30:00-07:00` and `2026-08-20T17:30:00.000Z`) resolve to
one instant through `_aware`, which is exactly the case `capacity.py:273` was written for.

---

# Plan: make the day planner cover the day, not the working window

Written 2026-08-09, from a concrete miss: the owner moved into Willow Hall 502 that
morning and the plan for the day contained breakfast, lunch, gym, shower, dinner and
three renderings of the same welcome dinner. Move-in appeared nowhere.

## Why it missed — five causes, all measured

The ledger knew. `commitment` #8, *"Move-in: Willow Hall 502, 8:00am (regular move-in —
Early Start early arrival declined)"*, due 2026-08-09, `status='open'`, confidence 1.0.
It reached the planner and the planner discarded it. Five separate mechanisms had to
line up for that, and each one is worth fixing on its own:

**A. Aug 9 was a Sunday, and Sunday has no working window.**
`working_days = [mon..fri]`, so `timezones.window_on` returned an empty window,
`capacity_minutes` came out 0, `cap.plannable` was False, and `propose` took the P3
early-return. All 48 candidates went to `overflow` and not one block was placed. Every
non-class obligation on every weekend of the term is invisible for this reason.
Evidence: `day_plan` id 18 — `capacity_minutes=0, planned_minutes=0, overflow_count=48`.

**B. The P3 message is wrong on a non-working day.** It says *"Fully booked — 0m of
capacity, under the 60m floor"*. The day was not booked; it was not a working day. Those
are opposite facts and the owner cannot act on the wrong one.

**C. P3 buries what is due.** docs/04 P3 says "list only what is due". The code assigns
the whole ordered candidate pool to `overflow`, so a 1.0-confidence obligation due today
sits somewhere among 48 rows with no rank the reader can see.

**D. A timed obligation that is not a calendar event has nowhere to put its hour.**
Move-in had "8:00am" in its text. `commitment` carries `estimated_minutes` and no clock
time — by design; `engagement` is the table that models a time-anchored thing, and
`capacity.engagement_events` already subtracts it. Extraction filed this one as a
commitment, so its hour was prose. Even on a weekday it would have been packed into the
first free slot at 10:00, not 08:00.

**E. All-day and multi-day engagements render nowhere at all.**
`engagement` #8 — *McKenna Summer Program*, 2026-08-09 → 2026-08-14, `confirmed` — is
skipped by `engagement_events` because it has a date and no clock time. The rule is
right that there is no honest hour to give it, and its rationale is about not silently
deleting capacity. Dropping it from the *display* was never the intent: the owner spent
six days inside a program the day plan never mentioned.

**F (noise, not a miss).** Today's plan drew the same dinner three times — 18:00–20:00,
18:30–19:30, 18:30–19:30 — from `engagement` rows 195, 183 and 184. `_distinct` collapses
only exact (title, start, end) triples, which is correct for its job of merging two
calendars describing one meeting, and useless against three extractions describing one
dinner.

## Already integrated — the ASU schedule needs no work

`capacity.fixed_events` selects `source LIKE 'calendar%'`, so both the 200 hand-imported
`calendar:asu` rows and the `calendar:apple` rows the connector now reads off the cleaned
Apple Calendar feed the planner today. Verified against live data, not assumed: connector
returns 18 events in its 21-day horizon, ledger holds the same 18 on those days,
`_distinct` merges 18 + 18 → 18, zero non-matching identities. Classes will subtract from
capacity exactly once from Aug 20.

## The work — DONE 2026-08-09, merged to main at f659684

Built on branch `planner-comprehensive` in a worktree (the shared checkout was on
another session's `fix/timeline-lane-packing` with dirty files; main was fast-forwarded
by ref update so that tree was never touched). Six commits, 1749 tests passing, ruff and
mypy clean, `scripts/truth.py` clean.

Ordered by how much of the miss each one removes.

### 1. [x] Plan seven days a week — `weekend_window` + config
`working_days` → all seven. A student's homework, move-ins, errands and program days do
not observe a Mon–Fri list, and the weekday gate is what took the whole day off the board.
Recommend a shorter weekend window (`WEEKEND_WINDOW=10:00-18:00`) rather than the weekday
10:00–22:00, so Saturday is plannable without pretending it is a workday.
*This one change is what makes Aug 9 plannable at all.*

### 2. [x] Separate "not a working day" from "fully booked", and lead with what is due
- `propose` distinguishes an empty window from a consumed one and says which.
- On the no-plan path, partition `overflow` into due-today/overdue and the rest, and
  surface the first group by itself. Honours P3's actual words.
- Tests: a Sunday with no window, a weekday genuinely consumed by meetings, and a day
  whose only due item is a 1.0-confidence commitment.

### 3. [x] Route timed obligations to `engagement` at extraction — prompt v8
`extract-commitments@8`: when the source states a clock time for an obligation the owner
must be somewhere for, emit an `engagement` with `starts_at`, not a commitment with the
hour in prose. Fixture set per the repo rule — move-in mail, a lab check-in, an advising
appointment, and two negatives ("submit by 5pm" is a deadline, not a place to be).
No schema change: `commitment` gaining a start time would duplicate `engagement` and
relitigate docs/03.

### 4. [x] All-day and multi-day engagements become visible without spending capacity
New `FixedEvent.kind = "allday"` emitted by `engagement_events` for a confirmed row with
a date and no hour, spanning `starts_at`..`ends_at`.
- **Subtracts zero capacity.** The existing rule stands; only the invisibility goes.
- Rendered as a banner on the plan, the schedule page and the brief's day section.
- Multi-day rows appear on every day they cover, marked "day 1 of 6".
- Tests: zero capacity delta; appears on all six days; a `proposed` row still appears
  nowhere (someone suggesting a week is not a week).

### 5. [x] Collapse near-duplicate engagements — the risky one, done last
Same local day + overlapping interval + a shared distinctive token in `what` → keep the
highest-confidence row, keep the others reachable. Deliberately at the engagement layer,
not by loosening `_distinct`, whose exact-triple rule is load-bearing for the two-calendar
case proven above. Flag for review rather than silent merge on anything below a wide
confidence gap.

## Execution notes

- **Worktree, not the shared checkout.** `state` reports `schedule.py`,
  `schedule.html`, `schedule_week.html`, `dashboard.css` and `test_schedule.py` dirty
  from another session. Do not sweep them into a commit — four lessons in this file are
  commit races from exactly that.
- **No `sync` for verification.** The last run cost ~$14 across three tiers. Use fixtures
  and the read-only connector probe used above.
- Items 2 and 4 touch templates; the deployed sidecar is *already* stale on
  `dashboard.css`. Rebuild `/Applications/Backglass.app` at the end or the owner will
  test a build that does not contain any of this.
- HTML assertions go through `tests/conftest.py::panel_slice`.

## What the review changed, and what is left

A fresh-context adversarial reviewer was run against the branch before merging. Capacity
arithmetic and timezone handling survived; the dedup rules and two of three reader
surfaces did not. Seven defects, all fixed in f659684, all silent deletions — a venue
merging the meetings held in it, two programmes annihilating each other, a multi-day plan
written with a clock eating a whole window, the dashboard printing `12:00am–12:00am`, the
week grid showing nothing, and a degenerate window making two pages contradict each other.
Worth keeping in mind: the first pass had eight tests over this code and every one passed.

Verified against the live ledger, not only fixtures. Sunday 2026-08-09 now proposes a
282-minute day with move-in on it; the Early Start programme runs as a numbered banner
across its days and takes nothing from any of them; the three dinners render as two.

- [ ] **Rebuild the desktop sidecar.** Deliberately not done in this session. The app
      freezes templates and CSS at build time and is already stale on `dashboard.css`,
      and this change touches `_today.html`, `schedule.html`, `schedule_week.html` and
      the stylesheet. But the shared checkout is on another session's branch with
      uncommitted work on the *same* surfaces, so a build now would ship neither their
      changes nor a tree that matches main. Rebuild once that work lands:
      `./desktop/build-sidecar.sh`, then copy the bundle over `/Applications/Backglass.app`.
- [ ] **`commitment` #8 is legacy data, and prompt v8 does not reach back.** "Move-in:
      Willow Hall 502, 8:00am" was a manual entry, so its hour is still prose and the
      planner places it at 11:30 rather than 08:00. Converting that row to an engagement
      is a one-line data fix and it is the owner's ledger, so it is offered rather than
      done. Future extractions take the engagement path.

---

# Plan: one reading, six ledgers, and a spine that makes them talk

**Goal (owner, 2026-08-09):** "make the app more comprehensive so that whenever new
information comes in the info will be analysed in a way such that planner, commitment,
decisions and roadmap can talk to each other, information processing isn't redundant,
this will also reduce confliction."

Read as three demands that turn out to be one problem: **cross-talk** (a new message
should be able to move a roadmap step, settle a decision, and change the day plan, not
only file a commitment), **no redundant processing** (the same truth must not be
re-derived by the model, and must not be re-implemented per surface), and **fewer
conflicts** (when two readings disagree, the system says so instead of picking silently).

## The diagnosis, measured against the live ledger

Six record types exist. **The pipeline writes two of them.**

| record type | rows now | written by the pipeline? |
|---|---|---|
| commitment | 164 open (138 `i_owe`, 26 `owed_to_me`) | yes |
| engagement | 212 (100 confirmed, 90 proposed) | yes |
| checkpoint | 4, **all `source='manual'`** | no |
| roadmap_step | 28, **0 done**, 26 pending with a planned date | no |
| decision | **0** | no |
| fact | 20, **0 with a `source_item_id`** | no |

Four numbers say the same thing four ways:

- **1 of 164 open commitments carries a `goal_id`.** The one column that already joins
  the commitment ledger to the goal engine is, in practice, empty. `PRIORITY_AT_RISK_GOAL`
  in `plan/planner.py:113` therefore fires for one row in the ledger.
- **0 roadmap steps have ever been marked done**, while 26 carry a planned date the
  planner has never seen. `backglass/plan/` contains no reference to `roadmap`. A step
  due next Tuesday is invisible to Tuesday's plan; the confirmation email that completes
  it is read, extracted, and cannot touch it.
- **Decisions are write-only.** `decisions.py` supersedes by title and can close one
  commitment; nothing else in the system reads the table — not triage, not the brief,
  not the planner, not the roadmap. So "I chose ASU" is recorded where nothing can act
  on it, while 95 other universities' admissions mail keeps arriving and keeps being
  paid for.
- **979 of 1,266 kept items (77%) produced no record at all.** That is the cost of a
  reader that can only answer one question. Some of those items were genuinely empty;
  an unknown share carried a decision, a fact, or a step advancement that the schema has
  a home for and the prompt never asks about.

### Where the redundancy actually lives

Two kinds, and they need different fixes.

**Redundant model work.** Triage and extraction re-decide from scratch what the ledger
already knows. The knowledge-base audit above measured 18 template shapes and 98 barren
extractions — about $17 imputed — on mail whose class the `fact` table could already
have ruled out. `{{owner_context}}` (done 2026-08-07) was the first half of the fix and
it carries *facts*; the durable rulings live in `decision`, which nothing reads.

**Redundant code.** "Is this sighting the same thing as that record?" is answered in
**nine** places, with nine policies:

| site | compares | on a match | writes? |
|---|---|---|---|
| `extract/commitments.py::_duplicate_of` | same `direction` **and** same counterparty entity, then `similar(what) ≥ dedup_threshold`, against this message's accepted list first and the open ledger second | cite the restatement | yes |
| `extract/commitments.py::_find_resolved` | model's `resolves_what` vs the open set, both directions | supersede | yes |
| `extract/engagements.py::_match` | ledger row or one accepted in this message | cite, link people | yes |
| `extract/engagements.py::_advance` | `ADVANCES_TO` state table | move status forward | yes |
| `extract/engagements.py:442` | `similar(...) < dedup_threshold` | reject a pairing | yes |
| `dedup.py::suspects` | ≥ 0.6, minus `commitment_distinct` | ask the owner | no (a page) |
| `plan/capacity.py::_distinct` | exact (title, instant, instant) triple | collapse for display | no |
| `plan/capacity.py::_one_plan_per_plan` | overlap (or equal span, for banners) + 2 shared 5-char tokens, all-day never merges with timed | collapse for display | no |
| `brief/daily.py:696` | commitment and engagement from the **same** `source_item_id`, `similar ≥ dedup_threshold` | suppress the engagement line | no |

Two things this table corrects about the obvious reading. **Five of the nine share one
knob** — `Settings.dedup_threshold`, default 0.85 (`config.py:240`) — so the sprawl is
in the surrounding rules, not in the numbers. And the read-side collapses **write
nothing**: `_one_plan_per_plan` says so in its own docstring. The defect there is not
deletion, it is that a collapse is invisible in the rendered day, which is a weaker claim
and still a real one — `tasks/lessons.md` records a venue merging the meetings held in
it, two programmes annihilating each other, and a reschedule double-booking a day.

What is genuinely wrong is that each of the nine was written where a bug bit, none knows
about the others, and adding four record types on top means four more bespoke matchers.

**So the refactor comes before the feature.** That ordering is the single most important
decision in this plan.

## The shape

```
   one careful read per item  ─────────────►  SIGHTINGS  (typed, evidenced, scored)
   (unchanged: one call, more keys)                │
                                                   ▼
                                        ┌──────────────────────┐
                                        │  resolve.py — ONE    │  new | restatement |
                                        │  identity + conflict │  advance | supersede |
                                        │  policy, six types   │  CONFLICT
                                        └──────────┬───────────┘
                                                   ▼
       commitment   engagement   checkpoint   roadmap_step   decision   fact
            │  │         │                        │
            │  └── prepares_for ──┘               │      (record_link carries these two
            └───────── advances ──────────────────┘       kinds and no others — L1)
                                          │
                     ┌────────────────────┼────────────────────┐
                     ▼                    ▼                    ▼
              day planner            morning brief        conflict page
        (one obligation pool)      (unchanged shape)    (nothing silent)
```

Nothing here is a vector store, a second careful pass, or an inferred goal. Those are
ruled out below and stay ruled out.

---

## Phase R — The resolution spine  *(refactor first, no behaviour change)*

The point is that after this, "have I seen this before, and what do I do about it" has
one implementation and one vocabulary, so the four new record types in Phase S cost a
policy table entry each instead of a new matcher each.

- [ ] **R1. `backglass/resolve.py`.** One algorithm, per-type policy. The policy is
      **four callables, not four constants** — a candidate query, a *gate* (the structural
      preconditions two rows must satisfy before similarity is even asked: same direction
      and counterparty for commitments, same all-day-ness for engagements, equal span for
      banners), a scorer, and an outcome function that may consult a state table
      (`ADVANCES_TO`). A shape of "threshold + query" cannot express the guards that are
      already load-bearing at two of the sites, and flattening them is how the guards get
      lost. Outcomes are a closed set — `new`, `restatement`, `advance`, `supersede`,
      `conflict` — and each is a written row somewhere, `conflict` included.
      **`settings.dedup_threshold` stays one knob.** Five sites read it today and
      `corrections.py:74` tells the owner to tune it; forking it into per-type numbers
      would change what an existing config field means inside a phase that forbids
      behaviour change.
- [ ] **R2. `conflict` table** (first migration in the ledger below): `kind`,
      `left_type`/`left_id`,
      `right_type`/`right_id`, `detail`, `source_item_id`, `status`
      
      (`open|resolved|dismissed`), `created_at`. A conflict is data, not a log line.
      Nothing in the spine is allowed to drop one side of a disagreement without
      writing here first.
- [ ] **R3. Migrate the five *write-time* sites onto it** — the two in
      `commitments.py`, the three in `engagements.py` — with the shared threshold and
      every structural gate unchanged, so the move is provably behaviour-preserving
      before anything is tuned. `dedup.py::suspects` and `brief/daily.py:696` are
      **read-side** and stay where they are; they are in the census so that a later
      threshold change is known to reach nine places, not five.
      **The two `capacity.py` collapses stay read-only and stay where they are.**
      They run inside `propose()`, whose contract is "Writes nothing — `persist` does
      that", and `_one_plan_per_plan`'s own comment says "Nothing is written". Writing a
      conflict row from there would either fire on every render or break that contract.
      Conflicts are recorded at sync/resolve time only; the overlapping-engagement check
      belongs to C2, which computes it on read.
- [ ] **R4. Evidence, uniformly.** `commitment_evidence` and `engagement_evidence` are
      the right shape; give `fact`, `decision` and `checkpoint` the same one so
      `source_item_derived.sql` — which today can only answer for commitments — can
      answer "what did this document produce" for all six types.

**Must not:** change any threshold, merge the two capacity collapses into the extraction
dedup (they answer different questions — two calendars describing one meeting is not
two readings of one invitation), or touch a prompt.

**Proof:** the whole suite green with no assertion edited; the six call sites' behaviour
pinned by tests written *before* the move, with a mutation proving each pin bites; the
`match-then-match` case from the 2026-08-02 lesson written for every type, not only for
commitments.

---

## Phase S — One reading, six record types  *(one call, staged one kind at a time)*

Cost is governed by output, not input (audit above: a 13,372-char extraction cost 5.7c,
a 7,822-char one 22.5c). So extra keys on the same call cost nothing on the items that
do not carry them, and a second careful pass would double the bill for the life of the
system — which is why prompt v4 folded engagements into the same call and why nothing
here adds a call.

Staged, **one kind per prompt version**, measuring the barren rate and the review-queue
reject rate between each. Asking for six things at once is how one item's evidence gets
attributed to another.

**Each stage's gate is three numbers, not two.** Barren rate, review-queue reject rate,
and **mean cents per call per tier** from `model_call`, against the version before it.
The keys are free on items that do not carry them, but the *prompt* grows on every kept
item, and rule 7 says a spend change is enforced in code rather than noticed later. The
telemetry already exists — `backglass state` prints it.

### The version bump is not free, and the plan has to say so

`pending_extraction_unbatched` selects on `extraction_version IS NULL OR != :version`,
so **every bump re-queues all 1,266 kept items** — roughly $220 imputed at the measured
17.5c extract tier, about $880 across four stages, against an audit whose headline
number was $94 of barren spend. It would not even be visible in one run: the ledger
already holds `@7` and `@8` side by side because the re-read drains over many syncs.

So each stage is **forward-only by default**: the migration stamps already-extracted
items with the new version, and only new mail pays. Reading history back is a separate,
deliberate act — **one** backfill, run once at the end of Phase S when the prompt is at
its final version, scoped to the items most likely to carry a signal (kept mail inside
the roadmap's date range) rather than to everything. Staging measures quality on new
mail; backfill buys history. Conflating them is how four accidental re-reads happen.

- [ ] **S1. `goal_signal` → v9.** The prompt exists
      (`specs/extraction-prompts/extract-goal-signal.md@2`) and has never been wired;
      `tasks/plan.md:386` already ruled it should ride as a field on the existing call.
      **It is not drop-in**: its own text says it "runs on kept items, after commitment
      extraction", so folding it in is a rewrite of that prompt (its own version bump)
      plus reconciling its `{{goals}}` context block into the commitments prompt.
      Writes `checkpoint` with `source='extraction'`; below threshold, review queue.
      **The stated idempotency needs a schema change**: `checkpoint` has only the
      non-unique `idx_checkpoint_target`, so "idempotent on `(target_id,
      source_item_id)`" is an assertion with nothing behind it until a unique index
      exists. Add it (migration below) rather than guarding in Python — rule 3 wants the
      second run to be unable to write, not merely unlikely to.
- [ ] **S2. `step_signal` → v10.** "This message says a roadmap step happened or is
      scheduled." **The model is not shown the step list and does not return a
      `step_key`** — it returns what the message says happened, with its evidence
      sentence, and `resolve.py` matches that against the 26 open steps afterwards.
      Injecting the live step list would make the same item extract differently as the
      roadmap changes, which turns every re-extraction diff into noise and puts the
      matching rules in a prompt instead of in the spine. Advancing a step goes through
      `adjust.py::complete_step`, which already writes the checkpoint — but **its
      signature has to change first**: it hardcodes `source="manual"` and passes no
      `source_item_id` (`adjust.py:53-59`), while `checkpoints.record` refuses
      `source='extraction'` without one (`checkpoints.py:79-80`). So either it gains
      `source` and `source_item_id` parameters, with every existing caller keeping
      `manual`, or an extraction-driven completion writes a provenance-free checkpoint —
      which is a rule 1 violation and re-creates the exact "4 checkpoints, all manual"
      symptom this plan opens by diagnosing. Take the signature change.
- [ ] **S3. `decision_signal` → v11.** "The owner has settled something." The first row
      the `decision` table will ever get from the pipeline — and the table cannot hold
      one yet: it has **no `confidence`, no `source`, no `source_item_id`**
      (`schema.sql:462`), so rules 1 and 2 are unsatisfiable without the migration listed
      below. Two hard constraints on top:
      **a model-written decision may not carry a `commitment_id`** until the owner
      confirms it — `decisions.record` closes the linked commitment in the same
      transaction and `revisit` deliberately does not reopen it (`decisions.py:126,167`),
      so an unconfirmed extraction naming a commitment would irreversibly drop a real
      ledger row; and it may not carry a scope (Phase D) for the same reason.
- [ ] **S4. `fact_signal` → v12.** Item C of the knowledge-base plan above, unchanged in
      intent: supersession rather than edits, provenance on every row, review queue below
      threshold. Last because it is the largest and the least urgent.

**Must not:** create a goal (docs/04 §2.1 — goals are entered by hand, and inferred ones
are uniformly wrong), create a roadmap or a step (only advance existing ones), link a
commitment to more than one goal (G8), or write any of these above threshold without an
`evidence` quote from the message itself (rule 1).

**Proof per stage:** the repo's fixture rule — real-shaped inputs with expected output,
including two negatives per kind; the whole fixture set run through one ledger in
pipeline order (the 2026-07-30 ordering lesson); barren-rate and reject-rate compared
against the prior version before the next stage starts.

---

## Phase L — The link spine  *(so a reader can ask one question and get the whole answer)*

**Trimmed after review, and the trim is most of the point.** The first draft defined four
link kinds; two of them (`settles`, `evidences`) had **no reader anywhere in this plan**,
and `evidences` duplicated R4's evidence tables outright. A link table whose initial
contents are a backfill of `commitment.goal_id`, `roadmap_step.target_id` and
`decision.commitment_id` is a second copy of joins that already work — which is the
"one truth, two homes" failure this plan opens by naming.

So `record_link` earns exactly the two kinds that have no home today and a named reader
in Phase P.

- [ ] **L1. `record_link`**: `from_type`, `from_id`, `to_type`, `to_id`, `kind`,
      `confidence`, `source_item_id`, `created_at`, `UNIQUE (user_id, from_type, from_id,
      to_type, to_id, kind)`. Two kinds only:
      `advances` (commitment|engagement → roadmap_step|target), read by P2's charge-once;
      `prepares_for` (commitment → engagement), read by P1's pool.
      A third kind requires a reader in the same change.
- [ ] **L2. Endpoint semantics, written down before the first row.** SQLite cannot
      foreign-key a type-tagged id, so nothing stops a link to a superseded decision or a
      cascade-deleted step (`roadmap_step` is `ON DELETE CASCADE` from `roadmap`, and
      `adjust.drop_roadmap` exists). Rule: a link is **ignored** when either endpoint is
      absent or not in an active status, and every reader goes through one function that
      enforces it. Per the 2026-08-02 lesson, grep every deleter of the referenced tables
      in the same change and assert the invariant from `PRAGMA foreign_key_list` rather
      than from a hand-written list.
- [ ] **L3. Backfill only what the two kinds need** — no model involved: a commitment
      whose `goal_id` matches a step's target; a commitment whose due date and
      counterparty match a confirmed engagement.
- [ ] **L4. One-hop only.** Every read is a single join. Two hops is a graph question,
      and this is not a graph.

**Must not:** replace the existing FK columns (`commitment.goal_id`,
`roadmap_step.target_id`, `decision.commitment_id` stay — they are the authority; the
link table is only for pairs that have no column), or gain a kind without a reader.

---

## Phase P — The planner reads the whole obligation set

Today `planner.candidates` (`plan/planner.py:96`) selects from exactly one record type —
`commitment`, joined to `source_item` only to date it. After this it reads one *pool*, and
P1–P10 are untouched — only what is eligible changes.

- [ ] **P1. Obligation pool.** Open `i_owe` commitments (as today) **+** roadmap steps
      with a planned date inside the horizon **+** cadence targets still owing sessions
      this week **+** preparation for a confirmed engagement that has a linked commitment.
      Every entry carries `(kind, id)`, minutes, and a due date, so `order()` and
      `select()` need no new concepts.
      **Where the minutes come from matters:** `roadmap_step` has no
      `estimated_minutes` column, and a pool entry of zero minutes is one `select()`
      packs infinitely. A step inherits `target.estimated_minutes_each` where it has a
      target, and otherwise takes the same default `estimates.backfill` gives a
      commitment with no estimate — extended to cover the new kinds in the same change.
- [ ] **P2. Charge each obligation once.** This is where cross-talk pays: a commitment
      that `advances` a step and the step itself are one hour, not two, and the spine is
      what knows. Without Phase L this widening would double-book the day — which is
      exactly why P comes after R and L.
- [ ] **P3. Make Monday's arithmetic and the daily plan agree.** `targets.capacity_check`
      (G5–G7) sums targets; the planner sums commitments. Point both at the pool so the
      Monday sentence and the Tuesday plan cannot contradict each other.
- [ ] **P4. `plan_block` gains the obligation's `(kind, id)`**, so completing a block
      writes a checkpoint against whatever it advanced, not only against a `goal_id`.

**Must not:** write to the real calendar (docs/11 cross-cutting rule 2), schedule an
`owed_to_me` commitment, or spend capacity on an all-day banner.

---

## Phase D — Decisions steer intake  *(this is the redundancy fix)*

The knowledge-base plan above parked item A — "facts reach the rule layer" — for a stated
reason: the facts are prose, and a rule needs a domain or an address, so turning one into
the other was either a new structured shape or an inference. **`decision` is that shape,
and it was already in the schema when the item was parked.** A decision is a settled
choice with a lifecycle, a supersession path, and a revisit button — everything a
suppression rule needs and a prose fact lacks.

- [ ] **D1. `decision_scope`** — one row per entry, not a JSON blob, and the grammar is
      **borrowed wholesale from `learned_noise`**: `kind IN ('address','domain')`, a
      lowercased `value`, `enabled`. Nothing else. "Template shapes" was in the first
      draft and is struck: tier 0 has no template-shape matcher, so it would have meant
      inventing a second grammar and a second matcher inside a phase whose whole argument
      is that one already exists. A decision with no scope row behaves exactly as today.
      New migration, never an edit to 0017 — shipped migrations are frozen bytes and
      `tests/test_migrations.py::FROZEN_CHECKSUMS` fails on any change to one.
- [ ] **D2. Scoped decisions join the tier-0 rule set** through the seam that already
      exists: `noise.enabled_entries` returns promoted values as a frozenset and
      `rules.classify(noise_senders=…)` already does address-equality and
      subdomain-aware domain matching over it (`extract/noise.py:96`, `extract/rules.py:79`).
      `decisions.scoped_entries(conn) -> frozenset[str]` is the same shape; `sync.py`
      unions it with `noise.enabled_entries` and `settings.noise_senders` at the one call
      site that builds `noise_senders`. No new rule code, no precedence question — the
      three sets are unioned, and a value's *provenance* is answered by asking each set,
      which is what the evidence page in D4 needs anyway.
      One ruling ("college chosen: ASU") replaces 142 per-sender promotions the owner
      would otherwise approve one at a time.
      **Throughput is the gate, not the rule.** D's payoff depends on the owner
      confirming model-written decisions, and the ledger holds zero decisions today. Stop
      condition: if S3 has not produced at least ten owner-confirmed decisions within a
      month of shipping, D is not blocked on more code — it is blocked on the signal, and
      the honest move is to say so rather than to loosen the confirmation gate.
- [ ] **D3. Decisions join facts in `{{owner_context}}`**, bounded the same way.
- [ ] **D4. Reversible and counted.** `revisit` un-suppresses. The noise page shows what
      each decision killed, with evidence, the way `noise_evidence.sql` already does for
      learned noise. A suppression nobody can see is the failure mode this whole feature
      is one bad row away from.

**Must not:** let a below-threshold or model-written decision suppress anything until the
owner has confirmed it. The asymmetry is deliberate and it is the one from the KB audit:
a wrong fact in a prompt is one line a model weighs; a wrong rule at tier 0 silently
deletes real mail.

---

## Phase C — The conflict surface

- [ ] **C1. `/conflicts` page + `backglass conflicts`**, reading the table Phase R
      writes. Each row names both sides, the evidence, and the two actions available.
- [ ] **C2. Cross-layer checks that only exist once the layers are linked:** a decision
      contradicting an open commitment; a step marked done whose target has no
      checkpoint; two confirmed engagements over the same hour with different people; a
      commitment due after its goal's target date; a fact contradicted by a newer
      extraction.
- [ ] **C3. One brief line, only when the count is non-zero.** Failures are louder than
      successes (docs/11 rule 4), and an always-present zero is noise.

---

## Sequencing, and why this order

**R → S → L → P → D → C.** R first because every later phase would otherwise add its own
matcher; S next because the new record types are what the links join; L before P because
widening the planner's pool without knowing what is the same obligation double-books the
day; D any time after S3 but stated late because it is the one with a silent-suppression
failure mode and it should land on a system whose conflicts are already visible; C last
because it reads what everything before it writes.

Phases R and S1 alone would already be worth shipping: one identity policy, and a goal
engine with an input.

### Migrations, in order, because the numbers freeze on first apply

A shipped migration is frozen bytes — editing one bricks every existing database and
fails `FROZEN_CHECKSUMS` in CI (2026-08-02 lesson). So the sequence is fixed here rather
than negotiated per phase. On-disk max today is 0019.

| # | phase | what |
|---|---|---|
| 0020 | R2 | `conflict` |
| 0021 | R4 | `fact_evidence`, `decision_evidence`, `checkpoint_evidence` |
| 0022 | S1 | `UNIQUE (target_id, source_item_id)` on `checkpoint` where `source_item_id` is not null |
| 0023 | S3 | `decision`: `confidence`, `source`, `source_item_id`, `status` already exists |
| 0024 | S4 | whatever `fact_signal` needs beyond `fact`'s existing provenance columns |
| 0025 | L1 | `record_link` |
| 0026 | P4 | `plan_block`: `obligation_kind`, `obligation_id` |
| 0027 | D1 | `decision_scope` |

If a phase is dropped or reordered, its number is **skipped, not reused**.

## Deliberately not

- **No vector store, no embeddings, no RAG.** CLAUDE.md's one idea, and it would not
  help: six typed tables keyed by id are a lookup, not a search.
- **No second careful pass per item.** Measured: extraction is 21× triage and cost tracks
  output. Every new record type rides the existing call.
- **No inferred goals and no inferred roadmaps.** Signals advance what the owner entered;
  they never create it.
- **No auto-resolution of a conflict.** The system's job is to notice and say so.
- **No new surface per record type.** A page per table is how a second brain becomes a
  filing cabinet, and this app already has nine pages. `/conflicts` is the one addition
  in this plan, and it earns it because a conflict row is a thing the owner can act on;
  facts, checkpoints and links get read through the surfaces that already exist.

## Definition of done

1. `uv run pytest` green, `ruff` and `mypy` clean, `uv run python scripts/truth.py`
   reporting zero STALE and zero ASK.
2. Two consecutive syncs against a frozen fixture write zero rows on the second — for
   every new record type, not only for commitments (rule 3).
3. A fixture message that completes a roadmap step advances the step, writes one
   checkpoint, and appears once — not twice — in the day plan.
4. A decision with a scope demonstrably drops a class of mail, and `revisit` demonstrably
   brings it back, with the evidence page showing the count both times.
5. Every new row carries provenance; every below-threshold row is in the review queue and
   in no other surface (rules 1 and 2).
6. **No regression in what already works.** Each Phase S stage re-runs the existing
   commitment and engagement fixtures and asserts the same records, with the same
   directions and dates, as the version before it. "Barren rate fell" cannot distinguish
   a better reader from commitments now mis-typed as facts, and that is the failure mode
   of asking one call for more kinds.
7. **The client-data boundary is checked per phase** (`docs/08`, CLAUDE.md rule 6). Four
   new record types, a link table and a scope table are six new places client-scoped
   content can land, and `truth.py`/ruff/mypy see none of it. `backglass purge-boundary`
   and the boundary doctor check run against a copy of the real ledger before each phase
   is called done.
8. A fresh-context adversarial verifier run per phase, because every phase here touches a
   merge or a delete, and `tasks/lessons.md` has seven entries about self-verification
   missing exactly that.

## Review trail

A fresh-context plan-verifier refuted the first draft (REVISE, eight objections) and a
reviewer flagged two more. What changed, so the next reader does not re-derive it: the
dedup census was six sites and is nine, five of which share one knob; `_one_plan_per_plan`
writes nothing, so "silent deletion" was overstated and the read-side collapses now stay
read-only; `complete_step` cannot write an extraction checkpoint without a signature
change; `decision` has no confidence or provenance columns at all; `checkpoint` has no
unique index to make S1 idempotent; the version bumps would have re-read 1,266 items four
times (~$880 imputed) with nothing saying so; `record_link` lost two of its four kinds for
having no reader; and `scope_json`'s "template shapes" would have meant inventing a second
matcher tier 0 does not have.

---

# Reach-out drafts: keeping in-person connections warm (2026-08-12)

A person met once in person leaves no trace in the ledger — no thread, no commitment, no
calendar block. `touch.py` can only measure silence it has evidence for, so the exact
relationships most at risk of going cold are the ones the system is blindest to. The
missing piece is not another detector, it is the next action: a written email the owner
can send in under a minute.

## What it is

`backglass reachout <person> --template <name> --note "<the specific thing>"` renders a
draft from a fixed template and prints it, with the evidence it was built from listed
beside it — never inside the email.

## Constraints taken as given

- **Deterministic, no model call.** Extraction is 30,593c of imputed spend; a template
  that fills slots costs nothing, is testable against fixtures, and calls no live API
  (testing rule). Phrasing is the owner's job, and the `--note` is where their voice goes.
- **Reads only.** The draft writes nothing. Sending it is what produces evidence: the
  reply lands through apple-mail like any other item.
- **Works with zero ledger evidence.** The in-person case is the primary case, not the
  edge. Name plus note must render a complete email; last-touch and commitments are
  optional enrichment.
- **Provenance beside the draft, not in it** (rule 1). Each line says which row, note or
  fact it came from.
- **Curated profiles only.** `people_cold.sql` requires role, org or a tag — a person
  created with a bare name is invisible to the warmth machinery this serves.

## Steps

- [x] `backglass/people/reachout.py` — templates, slot fill, `Draft` with evidence.
- [x] `reachout` CLI command, name-or-id resolution, `--json`.
- [x] Person page panel: template picker + note, returns the draft fragment with a
      `mailto:` when an address is known.
- [x] `tests/test_reachout.py` — fixture drafts, the zero-evidence case, the note line,
      no-writes assertion.
- [x] Felipe Batalini created as a curated profile and his draft produced.

## Done means

`uv run backglass reachout "Felipe Batalini" --template thanks --note "..."` prints a
sendable email containing the appreciation line, the ledger is unchanged after it runs,
and the same draft renders on `/people/<id>`.

## Follow-up: the reminder half (2026-08-12)

The draft was half the feature. `follow_up_section`'s own docstring names the other
half: *"a curated profile with no interactions ever has no evidence to cite, so it
appears on the People page but never here — a claim with no source does not ship."*
That is precisely the in-person connection, so the brief could never nag about the one
kind of relationship that has nothing but memory holding it up.

The fix is not to relax rule 1. It is to **create the evidence**: a touch the owner
records is their own claim, and quick-add already establishes the pattern — the owner's
words become a manual `source_item`, and the record cites it.

- [x] Migration 0023: `touchpoint` (entity, kind, when, note, `source_item_id NOT NULL`)
      and `entity.touch_every_days` — a per-person cadence, NULL meaning the global pair.
- [x] `people_cold.sql`: last touch is the newest of commitment evidence and touchpoints,
      so the provenance columns stay uniform and the brief's assert keeps holding.
- [x] `touch.py`: warn at the cadence, cold at twice it; the settings pair is the default.
- [x] `touch.record()` — the manual source item, the touchpoint, idempotent per day.
- [x] `backglass reachout log`, and `--due` for what is owed today.
- [x] Person page: "I met them" and "I sent it", full-page redirects like `/edit`.
- [x] The brief and the sidebar count need no change — both read `needing_follow_up`,
      which now sees these people.

Out of scope, deliberately: no snooze (logging a touch resets the clock, editing the
cadence covers "not now"), no new launchd job and no push channel (the 06:00 brief and
the sidebar are the delivery mechanisms, and both already exist).

---

# Periodic targets: the reminders nobody sends you (2026-08-14)

## Why

Audited Canvas today. It is fully wired and working — `canvas_ics.py` on the ICS
fallback (ASU disables student tokens), 4 assignments → 4 open `i_owe` commitments,
provenance intact. Nothing to fix there beyond a transient `ConnectionResetError` that
clears itself and an `audit_sources.py` label false positive.

The gap is the other half of the question. "Apply to internships this cycle." "See your
advisor — it has been six months." "Start looking for a research placement." None of
these exist anywhere in the ledger, and they cannot: **nobody emails you to say you are
overdue.** No Canvas assignment carries them, no inbox generates them, and the goal
engine cannot hold them either, because a target has exactly three kinds and none of
them repeats on a multi-month clock:

| kind | shape |
|---|---|
| `cadence` | a **weekly** count — `weekly_count` |
| `total` | a lifetime accumulator — Shadowing 60h, Research 200h |
| `milestone` | one dated event |

A six-month obligation is none of those. Modelled as a milestone it fires once and dies.
Modelled as a cadence it demands a weekly count that does not exist. So it goes
unmodelled, and the one class of college obligation with the longest lead time and the
worst consequences for missing it is the one class the system is blind to.

## The shape

`touchpoint` / `entity.touch_every_days` (migration 0023, two days ago) already solved
this problem for people. Same shape, reused deliberately:

- a cadence in **days**, not months — one primitive, one arithmetic, and `touch_every_days`
  already set the precedent. "Every 6 months" is 182 days and the drift is irrelevant at
  that scale.
- **due** at `every_days`, **overdue** at twice it. One number for the owner to choose
  rather than a warn/cold pair, which is the ruling touch.py already made.
- the clock is reset by a **checkpoint**, so progress still comes from checkpoints and
  never from a stored counter (G3, G10).
- anchored at the last checkpoint, falling back to `target.created_at`. A target created
  today is not instantly overdue — touch.py's "never interacted is young data, not a
  lapsed relationship", applied to obligations.

## Steps

- [x] **Migration 0024** — `ALTER TABLE target ADD COLUMN every_days INTEGER`. NULL for
      every existing row; only `kind='periodic'` reads it.
- [x] **Pre-empt the migration guards** rather than discovering them one test run at a
      time (2026-08-12 lesson): add the file's sha256 to `FROZEN_CHECKSUMS`, and regen
      `specs/schema.sql` with `uv run python -m tests.test_schema_reference` — generated,
      never hand-edited. No new table, so the `REFERENCES source_item` / `REFERENCES
      entity` enumerations do not apply.
- [x] **`goals/targets.py`** — `TargetProgress` gains `every_days`, `days_since`,
      `level` (new|fresh|due|overdue). `complete` is true for a periodic target that is
      not yet due. `weekly_minutes` returns 0: a semiannual obligation is not weekly
      load and must not distort the §2.3 capacity check.
- [x] **`goals/checkpoints.py::_default_target`** — exclude `periodic`. Today the fallback
      is `ORDER BY CASE kind WHEN 'cadence' THEN 0 ELSE 1 END, id`, so a goal whose oldest
      active target is periodic would have its advising clock silently reset every time
      any goal-linked commitment is resolved. Wrong row, no error — the 2026-08-12 failure
      mode exactly. A test pins it.
- [x] **`brief/daily.py::goal_section`** — the delivery mechanism. Emit a line only when
      due or overdue, with the day count in the text (G12: a day count, never a bare
      colour), `LedgerRef` provenance, silent when fresh (B3). The branch goes **before**
      `if not target.weekly_count: continue`, which would otherwise skip every periodic
      target in silence.
- [x] **`brief/weekly.py`** — `continue` for periodic in "Last week", commented. A week is
      not the unit a six-month obligation is scored in, same reasoning the `total` and
      `milestone` branches already carry.
- [x] **CLI** — `goals add-periodic <goal_id> <title> <every_days>` mirroring `add-total`,
      and `goals did <target_id> [--on YYYY-MM-DD]` writing a `source='manual'` checkpoint.
      `--on` reuses touch.py's bare-date → local-noon validation so a non-date cannot land
      in a column the readers sort by.
- [x] **Goals page** — the chip, so the dashboard shows what the brief says.
- [x] **Seed the actual reminders** the owner asked for, on the medical goal: academic
      advising (182d), internship applications (365d), research placement (365d).
- [x] **Tests** — new/due/overdue including the `created_at` anchor; `_default_target`
      never returns periodic; daily brief prints when due and is silent when fresh; one
      Phoenix ↔ Kolkata day-count case.
- [x] **docs/04** — the kind table.
- [~] **Rebuild the sidecar.** Migration 0024 applies to the shared db on the next launchd
      sync, and `/Applications/Backglass.app` was frozen before it: the next launch dies
      with `MigrationError: schema_version records migration(s) 24 that are not on disk`.
      This is the 2026-08-13 lesson on a delay fuse — the app keeps serving until it is
      restarted, and then it does not start.

## Out of scope, deliberately

- **No launchd job, no push channel.** The 06:00 brief and the goals page are the delivery
  mechanisms and both already exist — the same scope-out the touchpoint feature recorded.
- **No planner or capacity integration.** A periodic target contributes 0 weekly minutes
  and does not compete for a block. If the owner wants advising on the calendar they add
  a commitment, which already works.
- **No web log endpoint.** `routes/goals.py:514/:551` filter by kind, so v1 logs from the
  CLI. The chip renders; the button is a follow-up.
- **`audit_sources.py`'s credential-label false positive** (`CanvasIcsConnector: no
  credential row exists yet`, when `canvas:ics` plainly has one) is a separate fix. Listed
  so it is not folded in silently — a warning nobody trusts is a warning nobody reads.

---

# Chat commitments that can close themselves (2026-08-14)

**Goal (owner):** "fully integrate messages with friends by dynamically deciding whether
or not a commitment still exists."

## The miss, measured

84 open commitments come from `imessage`, across 16 monitored conversations. **79 of them
have later messages in the same chat**, and not one has ever closed from one. Two
`imessage` rows are `superseded`, one is `done`; everything else is open, some since
2026-05-06.

The reason is structural, not a bug. Closure today has exactly one path: prompt v9's
`resolves`/`resolves_what`, which fires only when a *new* message announces that it
completes an earlier promise ("here's that deck I owed you"). Mail works that way. Friends
do not: "bring dress shoes" is answered by bringing dress shoes, and the thread moves on.
So a forward-only signal can never reach the obligations that live in chat, and the board
fills with dead favours — "come over", "pick them up", "bring bedsheet to wash" — which is
exactly the noise that makes an owner stop trusting a board.

The read is therefore **backwards**: given a promise and the conversation that happened
*after* it, is the promise still live?

## Shape

One pass, `backglass/extract/recheck.py`, after extraction.

**Batched per conversation, not per commitment.** One call is given a chat's open
commitments *with their ledger ids* and the messages since that chat's last check. 16
chats is the ceiling; 84 per-commitment calls at the measured 17.5c extract tier is not,
and the same conversation window would be re-sent once per commitment. This is not the
"second careful pass per item" the backend plan rules out — it is one call per
conversation per sync, on conversations that have said something new.

**Ids in, ids back.** Because the model is handed the open list with ids, its verdict is
keyed by id and no similarity matcher is needed — no tenth entry in the dedup census, no
dependency on Phase R. Two guards from the 2026-08-12 wrong-table-id lesson: a returned id
is intersected with the set actually sent, and `status = 'open'` is re-read at write time.

**Silence is not evidence.** A closure must cite a message. Each line in the window is
printed with its `source_item.id`, and a `done`/`dropped` verdict must name one of them
plus a verbatim quote; a verdict with no citation is discarded, not applied. "Nobody
mentioned it again" is exactly the reasoning that would close every real obligation the
owner has been quietly failing to do.

**Asymmetric by consequence,** the same asymmetry step 4 of `commitments.apply` already
uses: above `confidence_threshold` the commitment closes through `actions.resolve`/`drop`
with the citation recorded; below it, the verdict is stored `pending` and shown on the
review fragment for one click. A wrong open row is visible and dismissible; a wrong close
is silent data loss, and this file has four entries about paying for that.

**Idempotent by watermark.** `monitored_chat.rechecked_through` holds the highest
`source_item.id` a chat has been checked through. No new messages → no call → zero writes,
which is rule 3's test.

## Steps

- [ ] 1. Migration `0024_commitment_recheck.sql`: `commitment_recheck` +
      `monitored_chat.rechecked_through`. Guard checklist first (2026-08-12 lesson):
      `REFERENCES source_item` test, `imessage.DEPENDENTS`, `FROZEN_CHECKSUMS`,
      regenerate `specs/schema.sql`.
- [ ] 2. `specs/extraction-prompts/recheck-commitments.md@1` + fixtures, negatives
      included: a still-open promise, a vague "sounds good", a group member closing
      somebody else's obligation, a plan that moved rather than died.
- [ ] 3. `RecheckVerdict` / `RecheckResponse` in `extract/schemas.py`.
- [ ] 4. `extract/recheck.py` — candidates, window, render, parse, apply.
- [ ] 5. `_recheck_pass` in `sync.py`: after extraction, per-chat `try/except` (rule 5's
      unit is the loop item), metered under a new `recheck` tier so `state` prices it,
      inside the same `SpendCap`.
- [ ] 6. `backglass recheck` CLI — `--dry-run`, `--chat`, `--json`.
- [ ] 7. Pending verdicts on the existing review fragment, with confirm / keep-open
      routed through `actions`. No new page.
- [ ] 8. `tests/test_recheck.py`: the fixture set through one ledger in pipeline order,
      the zero-writes second run, the uncited verdict, the foreign id, the
      already-closed commitment, both sides of the threshold.

## Deliberately not

- **Commitments only.** Engagements have `advance_engagement` and a status machine of
  their own; giving them a second closer is how two policies disagree.
- **No merge or dedup verdicts.** The duplicate clusters in this ledger are re-extraction
  artefacts, and closing the underlying obligation clears them. Merge heuristics are the
  most-relitigated thing in this file.
- **No new page.**
- The 10 undecided iMessage conversations stay undecided; that is the owner's call on
  `/chats`, not something this pass should widen.

## Built, 2026-08-14

Migration 0024 is applied to the live ledger and three targets are seeded on goal 1:

| id | title | cadence | first fires |
|---|---|---|---|
| 59 | Academic advising check-in | 182d | 2027-02-13 |
| 60 | Internship & summer research applications | 365d | 2026-11-02 |
| 61 | Research placement review — is this still the right lab | 365d | 2027-08-14 |

Proved against the owner's own db, not a fixture: `goal_section` is silent on
2026-08-14, prints the internship line on 2026-11-02, and prints both it and advising
on 2027-02-13. 1,951 tests pass.

Target 59's clock is anchored at creation, so it first speaks in February. If the owner
saw an advisor recently, `backglass goals did 59 --on <date>` moves the anchor to the
truth; if it has already been six months, backdating brings it forward instead.

Target 60 is anchored at 2025-11-01 deliberately, so a 365-day cadence lands on the
opening of the application cycle rather than 365 days after the day this was built.

**Left undone: `/Applications/Backglass.app` is still the old bundle.** The new one is
built, signed and verified to carry both migration 0024 and the updated card template
(`desktop/src-tauri/target/release/bundle/macos/Backglass.app`). Installing it means
quitting the app that is running right now, which is the owner's call. Until then the
running process keeps serving and dies on its next launch — the 2026-08-13 fuse.

## Found, not fixed

`tests/test_reachout.py::TestTouchOnThePage::test_recording_a_touch_moves_the_chip`
fails on `main` and failed before any of this — confirmed by stashing. Unrelated to
periodic targets; it belongs to the touchpoint work from two days ago.

---

# Audit: the daily planner, and why its failures are not in the planner (2026-08-15)

Asked to improve the app, improve how it gets source information, and audit the daily
planner. Those turned out to be one question. The planner's code is largely sound; what
is broken sits on either side of it — the machine it runs on, and the ledger it reads.

## The finding that outranks the rest: the morning surfaces do not run in the morning

Both scheduled jobs fire, and both fire late enough that they miss the thing they exist
for.

| job | scheduled | last actually ran |
|---|---|---|
| `com.backglass.plan` | 05:45 | **17:22** |
| `com.backglass.brief` | 06:00 | **17:37** |
| `com.backglass.backup` | 02:00 | 13:39 |
| `com.backglass.shutdown` | 22:00 | 09:33 |

Read from the log mtimes, and corroborated by the ledger: `day_plan` 36 covers
2026-08-15 and was generated at `2026-08-16T00:22Z` — 17:22 Phoenix. The day it plans is
over.

The cause is not launchd misconfiguration. `StartCalendarInterval` fires on the next
wake when the machine was asleep at the scheduled time, and this machine is asleep at
05:45. So the morning brief — the product's first surface, the two-minute read at 06:00
that `docs/01` opens with — is silently not delivered on any night the lid is shut. It
arrives at dinner.

`plan-catchup` was built for exactly this and does not cover it: it is `RunAtLoad`, so it
fires at **login**, not at lid-open wake. A machine that sleeps and wakes never triggers
it, and the late calendar job is what produces the plan instead.

Worse, the two can fight. `plan --if-missing` exits without writing when a live plan
exists, which is what makes catch-up safe. The 05:45 job carries no such flag. So a
catch-up that does produce a morning plan can be superseded at 17:22 by a fresh one whose
blocks are all in the past.

**The planner has no concept of "now".** `propose(conn, settings, day)` takes a date and
plans the whole working window from the start of it. Run at 17:22 it emits a 07:30
breakfast and a 10:00 protected block. Nothing is wrong with the packing; it is answering
a question about a day that already happened.

## What the ledger hands the planner is not schedulable

Today's plan and its overflow list, verbatim from `data/plan.log` and `data/plan.err`:

```
1:15pm–1:45pm  review agreement form and accept scholarship award
1:45pm–2:30pm  accept Academic Excellence Scholarship award
3:15pm–4:00pm  complete AES scholarship acceptance and agreement form
```

```
· did not fit: submit volunteer application on HOV website (45m)
· did not fit: submit volunteer application online (45m)
· did not fit: Submit Hospice of the Valley volunteer application at volunteers.hov.org (45m)
· did not fit: Reply sent to HOV — confirm next college orientation date and submit before it (15m)
· did not fit: Log 6 lunch conversations into Backglass (45m)
· did not fit: Log the six McKenna lunch conversations — name + one thing each (15m)
```

One scholarship acceptance is booked as three blocks and eats two hours of a 281-minute
day. One volunteer application appears four times in the overflow. `commitment_distinct`
holds 7 rows against 287 open commitments, so the dedup path exists and is barely used.

**The planner is faithfully scheduling duplicates, and any timing fix ships that
faithfulness earlier in the day.** This is why "how it gets source information" and "audit
the planner" are the same work item: the planner is a report over the ledger, and the
ledger has the same obligation written down three ways because three emails mentioned it.

## The capacity numbers are arithmetic over invented inputs

| estimate_source | open commitments | mean minutes |
|---|---|---|
| `type_default` | **266** | 44.5 |
| `manual` | 17 | 78.8 |
| `extracted` | 4 | 8.75 |

256 of 287 open commitments are estimated at exactly 45 minutes, because `classify()` in
`plan/estimates.py` has four patterns — `meeting_prep`, `decision`, `review`, `draft` —
and the backlog is made of verbs none of them match: *submit, apply, complete, call,
email, log, RSVP, register, attend, pay*. Everything falls to `unknown` → 45m.

So "77 item(s) did not fit" means 77 × a number nobody chose. The overflow count, the
capacity gap and the Monday "something has to give" sentence are all real arithmetic over
a fabricated column.

**The correction loop for this is dead.** `ratio_report` compares estimated against
actual over blocks marked `done` and needs 30 of them. Across 36 plans and 445 blocks
there is exactly **one** `done` block; 429 are still `pending`. It will never reach
sample. And `docs/04 §1.3` forbids auto-retuning, correctly — so the only path is
widening the type table by hand.

## Nobody is using the output

- **0 of 36 day plans have ever been accepted** (`accepted_at IS NULL`, all `proposed`).
- **1 of 445 blocks has ever been marked done.** 429 `pending`, 15 `rolled`.
- 106 of the 122 dated open commitments are already overdue.

This is the number that should govern what gets built next. A planner nobody accepts and
a block nobody ticks is not a planner with a ranking problem. Before adding capability,
the honest move is to make the thing arrive when it is useful and stop it proposing work
that is already done or duplicated — which is P1 and P2 below, and nothing more clever.

Note also that `checkpoints.from_completed_block` only fires on `outcome='done'`, so the
goal engine receives essentially no signal from the planner at all.

## The source side

- `apple-notes` last produced an item **16 days ago** — a parked cursor, per the audit
  script's own warning. Worth the skill's 6-step debug.
- **Embedding backlog is growing**: 36 documents unindexed, up from 8 last week. Retrieval
  is additive by CLAUDE.md's ruling, so nothing is wrong-answering, but the gap widens.
- `facts` has 23 rows and **`with_provenance: 0`**. Every durable fact about the owner
  carries no `source_item_id`. That is rule 1 unmet in the knowledge base;
  `people/touch.py`'s manual-source-item pattern is the template for fixing it.
- `calendar:asu` holds 200 items with no connector — nothing will ever refresh them.
- `audit_sources.py` still reports `CanvasIcsConnector: no credential row exists yet`
  when `canvas:ics` has one. Carried from yesterday.

## Ranked plan

- [x] **P1 — the morning surfaces fire in the morning.** Add `--if-missing` to the 05:45
      `plan` template so a late firing can never supersede a catch-up's morning plan, and
      have the every-1800s `sync` job (which *does* run on wake) backfill the brief and
      the plan when they are missing and their hour has passed. Templates live in
      `launchd/templates/`, are generated, and are applied with `backglass schedule
      install` — never hand-edit the installed plists. A true 06:00-with-the-lid-shut
      needs `sudo pmset repeat wake`, which is the owner's machine-level call and is
      offered, not done.
- [x] **P2 — dedupe the backlog.** `backglass commitments dedupe --dry-run` prints
      clusters and writes nothing. Applying is gated on the owner reading the list: this
      is live-db surgery on 287 rows, and the 2026-08-12 lesson is that a wrong-table id
      validates and writes to the wrong row. Print the exact rows before any write, use
      commitment ids only, and close duplicates through the existing supersede path with
      a note naming the survivor. Extraction-side prevention is a follow-up, not this
      round.
- [x] **P3 — teach `classify()` the verbs this ledger actually uses.** submit, apply,
      complete, call, email, log, rsvp, register, attend, pay. Each new kind needs an
      entry in `settings.estimate_defaults`. Nothing cleverer — the feedback loop is dead
      and the docs forbid silent retuning.
- [ ] **P4 — source hygiene**: the parked `apple-notes` cursor, the growing embedding
      backlog, and the `audit_sources.py` label false positive.

Deliberately not this round: no schema migration (nothing above needs one, and each one
re-arms the sidecar fuse), no planner rewrite, no feature built to manufacture engagement.
Open question, not investigated: several days show `capacity_minutes: 0` while still
persisting 19 blocks.

## Built, 2026-08-15

**P1 — the deferred run now plans the day that is left.** The fix is not the one the
audit proposed. Adding `--if-missing` to the 05:45 job was tried and reverted: a test
already pins that job to always regenerate, and names why — it is the run built on the
overnight batch collect, and it has to be able to replace a thinner plan a pre-dawn login
wrote. Skipping the run was never the right answer.

The defect was that `propose()` had no concept of *now*. It took a date and packed the
whole working window from the start of it, so a run launchd deferred to 17:22 emitted a
07:30 breakfast. `capacity.compute` now takes `not_before` and `propose` takes `now`,
clamping the window to the hours that remain. It defaults to None, so every test and
every what-if still measures a whole deterministic window; only the callers that know the
wall clock — the CLI and the catch-up net — opt in.

`backglass/catchup.py` is the net proper, hung off the every-1800s sync job because that
is the only scheduled job that runs on wake. It fills a hole and never replaces anything:
a live plan or an existing brief is left alone, and it never fires before the hour the
surface is owed at. It generates the brief and deliberately does not send it — the owner
agreed to receive mail at 06:00, not whenever a sync noticed the gap.

`plan_at` joined `brief_at` in settings; 05:45 was frozen in the launchd template, which
is the exact drift `schedule.render` exists to prevent, and it is now also the hour the
net measures "the morning already passed" against. One number, one place.

*Found while verifying on the live ledger:* the clamp made `no_window` lie. It was derived
from `window_minutes == 0` on the documented grounds that only the non-working-day branch
could produce that — and the first thing the clamp did was report a Saturday the owner
works as "not a working day". A zero window now has three causes, and `window_closed` is
the third: *"The working window closed at 18:00 — nothing left to plan today."*

**P3 — estimates, measured rather than guessed.** 273 of 287 open commitments classified
as `unknown`. Counting the leading verbs of that set gave the five new types — message,
call, form, errand, log — and `send`, the single most common verb, was missing from the
first draft until a test caught it. `backfill` now re-derives rows already marked
`type_default` as well as filling NULLs, which is what makes a change to the table
actually reach the backlog; manual and extracted estimates are still untouchable.

| | before | after |
|---|---|---|
| open commitments at exactly 45m | 256 of 287 | 77 |
| estimated backlog | ~215 hours | 154 hours |

**P2 — clusters, not pairs.** `dedup.suspects` was already finding these; it returned 264
open pairs and every duplicate costing planner time was in there. Its own docstring named
the failure — *"a queue of 261 is one nobody reaches the end of"* — so the gap was the
review surface, not the detection. `backglass duplicates` collapses the suspect graph into
connected components: **264 pairs became 53 clusters over 215 commitments.**

It writes nothing without `--apply`, and `--apply` only touches clusters where every
member is the same sentence *and* the shape is not a fan-out. That guard is not
theoretical: 4 of the 6 identical-text clusters on this ledger are one message promising
an intro email to three different instructors, and collapsing them would have destroyed
eight real commitments.

## Found while building, not fixed

- **Entity duplication is upstream of some of these clusters.** `UW–Madison Financial Aid
  Office` and `University of Wisconsin–Madison` are one organisation the `entity` table
  holds twice, and the pair wears the fan-out shape because of it. `backglass people
  merge` fixes the cause. Worth a pass — it would resolve several clusters without any
  judgment about the commitments themselves.
- **A working day with a full window and zero capacity.** 2026-08-14 was a Friday with a
  720-minute window and `capacity 0m`. Fixed events consumed all of it. Not investigated;
  it is the audit's open question and it is real.
- **The planner still has no consumer.** 0 of 36 plans accepted, 1 of 445 blocks marked
  done. Nothing built this round changes that, deliberately — the honest first move was to
  make the plan arrive while the day is still ahead and stop it proposing work that is
  duplicated or mis-sized. Whether it then gets used is the question the next round should
  ask, and it should be asked by looking rather than by building.
