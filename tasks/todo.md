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
