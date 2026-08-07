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
