# The full picture: memory architecture (goal set 2026-08-18)

The owner's directive: the app must give the AI the full picture of what is happening in
their life — short-term memory, long-term memory, a knowledge base about the main people
— and on that structure, recognize things on its own (an outdated commitment, school
priority overriding gym time), understand preferences, notify, and re-plan the day
dynamically.

**Stated assumption, so it is not invisible:** "redo the entire backend logic" is read as
*build the capabilities*, not tear down the decided architecture. CLAUDE.md's
decisions-already-made table (SQLite, two-tier extraction, immutable ledger, typed
records primary) stays closed; everything below is additive restructure. The memory
tiers are **context assemblers over the tables that already exist**, not new stores —
the ledger stays primary, and a second copy of the truth would drift from the first.

## What already exists, mapped to the goal

| Goal component | Existing surface | Gap |
|---|---|---|
| Long-term memory | `fact` table, `facts.owner_context` | pipeline writes zero facts; context reaches triage only |
| People knowledge base | `entity`, `touchpoint`, `people/` | never reaches a model call |
| Short-term memory | — | nothing assembles "what is happening right now" |
| Outdated-commitment recognition | `extract/recheck.py` | chat only; mail/board decay unhandled |
| Priority conflicts | `open_question` (kinds: conflict, priority) | detectors exist; preferences don't inform the planner |
| Preferences | `fact` rows under `preferences/` | planner never reads them |
| Notifications | — | greenfield |
| Dynamic scheduling | 05:45 plan + catchup net | fills holes only; never re-plans on drift |

## Increments (each lands tested + committed before the next starts)

- [x] 0. Baseline: commit inherited state-verdicts + catchup work (2acf92d).
- [x] 1. **`backglass/context.py`** — one assembler, three tiers, char-budgeted,
      deterministic, empty-per-section when there is no data:
      1. long-term: `facts.owner_context` (exists, reused);
      2. people: the handful of entities with recent evidence — role, org, tags,
         last touch;
      3. short-term: a deterministic report over the ledger — overdue and due-soon
         commitments, today's plan and engagements, open questions.
      No embedding dependency: retrieval stays additive, never load-bearing.
      Wire into triage (same `{{owner_context}}` placeholder — richer content, no
      version bump) and into extraction via **v10 + `compatible:` frontmatter**: a new
      prompt version whose frontmatter names v9 as still-valid, so the 1,300-item
      ledger is NOT force-re-extracted; new items get the new prompt. This decouples
      "the prompt improved" from "everything must be re-read", which v9's history
      complained about in a comment.
      landed (e39fd8c): `context.assemble` (short-term + people over the ledger, long-term via
      `facts.owner_context`), `compatible:` frontmatter + `Prompt.stamps` +
      `:compatible_versions` in both pending queries, wired into triage/batch-triage
      (assembled context) and extraction v10 (`{{owner_context}}` in the dynamic half),
      `backglass context` CLI. 21 tests.
- [x] 2. **Fact writeback** — extraction (same v10 bump) emits fact candidates with an
      evidence quote + `source_item_id`. Quote verified against the body (recheck's
      citation rule); verified + confident → `fact` active via `remember()` (which
      supersedes); unverified or hesitant → `status='proposed'`, surfaced for review,
      **invisible to `owner_context` until accepted** — an extracted fact feeds every
      future model call, so one hallucination would poison the pipeline; the gate is
      the point. Same-value re-emission is a no-write (rule 3). Migration 0026 adds
      `confidence` to `fact`; run the new-table checklist (FROZEN_CHECKSUMS, schema
      regen) BEFORE the first suite run; sidecar rebuild re-armed.
      landed (17b27b1): `ExtractedFact` in schemas v10, `facts.propose()` + citation gate in
      `tier2.apply`, `facts accept/reject` CLI, proposed-facts review on the Memory
      page, migration 0026 (`confidence REAL`), 19 tests.
- [x] 3. **Staleness beyond chat** — recheck covers conversations; the mail-shaped
      version is decay: a commitment overdue N days with no later evidence, or
      superseded in substance by a newer one, becomes a *question or review item*,
      never an auto-close — in mail, silence is even weaker evidence than in chat.
      Auto-close continues to require a citation (recheck's rule, unchanged).
      landed (6dd613b): `_stale_commitments` detector in questions.py (overdue ≥14d + evidence
      silence ≥14d, per-commitment ask-once identity, newest evidence cited, batch
      answers via existing questions UI). 8 tests.
- [x] 4. **Preferences → planner** — preference facts (`preferences/…`) get a typed
      lane the planner reads in `order()`/`select()`: protected blocks (gym, sleep),
      priority order between lanes (school > social), owner-stated rules. A conflict
      the rules cannot settle raises an `open_question` (surface exists) rather than
      guessing — never guess in the meantime.
      landed: `plan/preferences.py` (`priority:` and `protect:` lanes parsed from
      `preferences/planner.*` facts with malformed-value guard), lane classification +
      preference-aware ordering in `planner.order()`, protected-lane conflict question
      via `questions.protected_conflicts` (ask-once, HORIZON_DAYS window). 21 tests.
- [x] 5. **Notifications** — a `notification` ledger table (idempotent: notify once
      per (kind, subject, day); provenance per rule 1; quiet hours), delivered from
      the sync path with catchup's owed-at pattern, via macOS `osascript` (the JXA
      bridge connectors already use). Timezone tests specifically — owed-at-an-hour
      is exactly the UTC-7/+05:30 surface the lessons cover.
      landed (85b08a1): migration 0027 (`notification` table, UNIQUE dedup key),
      `backglass/notify.py` (deciders: overdue-today, plan-replaced, stale-questions;
      quiet hours 08:00–22:00 via active-tz local time; osascript delivery best-effort,
      row is provenance), `notify_pass` in sync after catchup, `backglass
      notifications` CLI. 22 tests incl. both-zones quiet-hours cases.
- [x] 6. **Dynamic replan** — persist a fingerprint of the inputs a plan was built
      from (open commitments + engagements + capacity); on sync, material drift +
      plan still `proposed` → regenerate and supersede; plan `accepted` (the owner
      touched it) → notify and ask, never clobber. This is the decided resolution of
      the conflict between "dynamic scheduling" and catchup's "never replace a live
      plan": *proposed plans are the system's and it may re-plan them; accepted plans
      are the owner's and it may only knock.*
      landed: migration 0028 (`inputs_fingerprint` on day_plan), deterministic
      fingerprint over (commitments, engagements, capacity, tz) computed inside
      `propose()`, `replan_pass` in sync (fingerprint drift → supersede+regenerate
      proposed plans; `plan-changed` notification via notify's dedup; accepted plans
      get `plan-drift` notification only). 12 tests.

- [x] 7. **A stale commitment leaves the plan, not just the board** — reported by the
      owner on 2026-08-18: "the AES things on schedule make no sense because i go to
      asu". The 13:42 plan for today gave four of its twelve blocks to dead
      college-admissions work — UT Dallas scholarship acceptance (due 2026-05-01, from
      `AES@utdallas.edu`), a UW–Madison waitlist form — while fact 5 records the owner
      enrolled at ASU. Two defects, one complaint:
      1. `planner.candidates()` (planner.py:96) selects every open `i_owe` commitment
         and `PRIORITY_OVERDUE` ranks the most lapsed ones *first, forever* (docs/04
         §1.5 rule 2). Increment 3's detector asks "still real?" about exactly these
         rows and the planner schedules them in the same pass — asking and asserting at
         once. 104 open commitments are past due; the detector clears 5 per refresh, so
         without a gate the board stays wrong for weeks.
         Fix: one shared predicate. Move the staleness SQL and its constants out of
         `questions.py` into `backglass/staleness.py`; `candidates()` drops stale ids.
         Not hidden — a gated row is a question, and `STALE_KEEP` ("still on my plate")
         overrides the gate so an answer puts it straight back in tomorrow's plan.
      2. `actions.set_block_outcome` (actions.py:435) writes `plan_block.outcome` and
         never touches the commitment, so "done" on the board leaves the row `open` and
         tomorrow schedules it again. Live proof: block 518 `done`, commitment 69
         `open`, re-proposed today. Fix: `done` resolves the linked commitment through
         `actions.resolve`, best-effort if another surface already closed it.
      Out of scope, noted not fixed: commitments 64/69/73/83/178 are five extractions of
      one UT Dallas obligation (dedup), and nothing consumes fact 5 to moot a whole
      institution at once (relevance). Both are separate passes.
      landed (bc423b4, on branch `stale-planner-gate`): `backglass/staleness.py` holds the
      predicate and the three option constants; `questions.py` re-exports and asks 5 per
      refresh; `planner.candidates` takes the unbatched set and `propose` notes how many
      were held; `STALE_KEEP` leaves the set permanently, and because
      `inputs_fingerprint` hashes the post-gate pool that answer is same-day drift, so
      `replan_pass` rebuilds a still-proposed board rather than waiting for 05:45;
      `set_block_outcome('done')` resolves the commitment behind the block. 13 tests, gate
      and KEEP override each proven by a red mutation run. Live probe (read-only, 2026-08-18):
      83 of 205 open `i_owe` rows held back, all five UT Dallas rows among them, remaining
      board is ASU work.

- [x] 8. **A logic checker, disposing rather than asking** — owner, 2026-08-18: "we also
      need a logic checker for commitments and questions, if something obviously doesnt
      make sense then dispose of it yourself." The boundary that keeps this from being the
      silent-data-loss failure four lessons cover: **positive contradiction, never
      silence.** recheck closes on a quoted later message; staleness never closes, it stops
      scheduling and asks; this closes only where the record disagrees with itself or with
      a recorded fact.
      Two layers, two commits.
      landed (93b7a4a) — `backglass/logic.py`, deterministic, no model, no migration: an
      obligation whose own text reports it done ("sent updated resume", "MCAT prep
      completed"), a "still real?" about a commitment no longer open, a "should it have
      come first?" about a day that ended, a collision or placeholder hour the calendar
      no longer holds. Runs at the end of sync ahead of detection; `backglass logic
      --dry-run` to read it. Near-miss tests are the file: "zip it up once done" is a real
      obligation, protected-time questions carry no day, priority questions are never
      mooted by re-detection. New `moot` status, revivable by `questions.refresh` — the
      owner's `dismissed` stays permanent. 16 tests. Live read-only probe: 14 disposals
      (8 reported-done, 3 dead priority questions, 3 dead conflict questions), no errors.
      landed (6ff82ba) — the model half: migration 0029 (`logic_check`, judged once per
      commitment),
      prompt `check-relevance@1`, `backglass/extract/relevance.py`, `backglass relevance`,
      a bounded slice per sync behind the same spend cap as recheck. A `nonsense` verdict
      must cite an active fact id AND quote the obligation's own source, both verified in
      code; ≥ `relevance_drop_confidence` (0.85, its own setting above the extraction
      threshold) drops with the citation in `resolution_note`, below it asks one question
      whose answer settles the `logic_check` row. 14 tests, every guard and both sides of
      the asymmetry, plus the dismissal path: waving a `nonsense` question away settles
      its `logic_check` row to `kept`, because the judge reads judged-once and a dismissed
      question with a pending verdict would strand the obligation unasked-about forever.
      Not yet run against the live ledger — it costs model calls, so the first pass there
      should be `backglass relevance --dry-run` after merge.
      Layer 1 verified end to end on a copy of the live 55 MB ledger: 0029 applied, the
      first pass disposed of 14 (8 reported-done, 6 dead questions) writing 14 decision
      rows, the second wrote nothing. Rule 3 on real data rather than fixtures.

- [x] 9. **Two surfaces the checker's own output broke, found by looking at it** —
      not planned; both came out of reading the live ledger after increment 8 landed.
      1. The disposals are `decision` rows, which was right for provenance and wrong for
         the page: 14 machine rows against the owner's 4 standing decisions on the first
         pass, banner count included. `decisions.active` now answers "what have I
         decided" and `decisions.disposals` "what did the checker throw out", split on a
         named `MACHINE_PREFIX`; the page shows both, the machine's below and quieter.
         Hiding them was never an option — a row that vanishes with nothing saying why is
         indistinguishable from a bug.
      2. `duplicates.clusters` built connected components, and similarity is not
         transitive. On today's ledger that produced one cluster of **37** — a UT Dallas
         scholarship acceptance, a hospice volunteering application, an enrolment fee and
         an AP-credit transfer — asked as one "same promise?" card. Now stars: a centre
         plus its direct suspects only, greedy by degree, ties by id. Measured on the
         copy: 50 cards → 60, largest 37 → 9, and the five UT Dallas rows stay one card.
         Greedy cliques were measured too (68 cards, largest 6) and rejected: they split
         that family across three cards, which is the opposite failure.
      Also fixed `tests/test_duplicates.py`'s dry-run test, which opened whatever ledger
      the ambient config named and so passed only in the owner's checkout. Suite runs
      green with nothing deselected (2192).

- [x] 10. **Landed on the live ledger, 2026-08-19** — owner said go ahead. Merged with
      the concurrent autonomy work (both answer hooks kept), main fast-forwarded to
      664a708 through `receive.denyCurrentBranch=updateInstead`, config restored; 2207
      tests green. `backglass logic` disposed of 15 and the second pass wrote nothing.
      `backglass relevance` judged all 198 unjudged obligations over four passes: 43
      dropped citing a fact, 5 asked about, 150 kept, 0 discarded. Open commitments
      276 → 226. Today's plan regenerated: no AES, ASU work only, with the held-back
      count in the notes. ~$6.50 across the four passes, of which `model_call` recorded
      only the sync-side one — the CLI's `Metered` calls list is never persisted (same
      shape as the recheck CLI, pre-existing, noted not fixed), so a cap audit will
      under-count CLI passes. One arguable drop: #215 "Submit FAFSA" (Simpson-sourced)
      against fact 5, while the ASU-side money rows #189 and #309 stayed open —
      tombstoned with its citation, one status flip to reopen.

## Constraints that bite

- Any migration re-arms the frozen-sidecar crash (`matches_source` already false);
  every increment that adds one notes the rebuild step in its commit.
- Prompt changes are version bumps in `specs/extraction-prompts/`; re-extraction is a
  deliberate owner decision (drop the `compatible:` line), never a side effect.
- Cost tracks output, not input (2026-08-07 audit) — richer context per call is
  affordable; do not pre-optimize it away.
- No test calls a live API; every new prompt gets fixtures.

## Goal 2 (2026-08-19): no terminal, everything looped, paths volunteer

- [x] 7. **Self-healing schedule** — `schedule.ensure_loaded()` bootstraps unloaded
      com.backglass plists at dashboard startup; `unloaded_jobs()` (None when
      launchctl unaskable); state verdict "every scheduled job is loaded". Root
      cause: sync plist on disk, never loaded — 13 silent hours on 08-18. (7990034)
- [x] 8. **Plan lifecycle on the page** — Accept / Replan / "Plan this day" buttons
      on /schedule; drift knock points at /schedule again. (7990034)
- [x] 9. **Roadmaps volunteer** — `signals:` preset frontmatter; roadmap question at
      ≥3 word-boundary matches over open commitments+plans; Start instantiates,
      decline never re-asks, any roadmap row (even dropped) means decided. (7990034)
- Commitments/facts/engagements autodetect: already automatic via the sync loop
  (goal 1). Questions-only-when-necessary: ask-once + floors + only-news banners.

## Goal 3 (2026-08-20): a situation the checker reads, and commitments that depend on it

The owner: *"make the logic engine better at understanding current situation and create an
evolving state doc about me and every commitment should have dependencies and if the
dependency interferes with state doc it should be deleted."*

Three asks, one mechanism. The reading below is deliberate and stated so it is not
invisible:

**Stated assumption 1 — "deleted" means tombstoned.** docs/03 makes commitments immutable
and `actions.drop` is the only close path. Every deletion here is a tombstone with the
dependency and the superseding fact in its `resolution_note`, a `decision` row, and one
status flip to reverse. Four lessons are about paying for a real deletion.

**Stated assumption 2 — the state doc is a rendering, never a second store.** The `fact`
table already is the claims layer: supersession, citation, status, confidence, a Memory
page. "Enrolled at ASU Tempe" *is* fact 5. A second store holding owner-state would drift
from the first, and CLAUDE.md's ledger-stays-primary rule forbids the doc becoming
load-bearing. So the doc is a versioned rendering over `fact` + `context._situation`, and
its evolution is free: facts supersede, the next rendering differs, the diff is the change.

**Stated assumption 3 — "every commitment has dependencies" includes "none".** "Zip it up
once done" depends on no recorded fact. Forcing a dependency onto it would invent the
citation that the citation-or-no-verdict rule exists to prevent, so `none` is a recorded,
first-class outcome — judged, depends on nothing, never re-asked.

### The gap this actually closes

`logic_check` is `UNIQUE (user_id, commitment_id)`: judged once, ever. A `keep` issued
when the facts said one thing is never revisited when the facts say another. That is the
owner's complaint restated in schema — the checker has no way to notice the situation
moved. Dependencies are the **invalidation index** that gives it one: fact 5 superseded →
look up its dependents → re-judge only those. Nothing else in the ledger can answer
"which obligations did this fact hold up?"

### Increment A — record dependencies, render the doc. No new deletion. (read-only)

- [~] A1. **Migration 0031** — `commitment_dependency`:
      `(id, user_id, commitment_id, dep_key, kind, fact_id, depends_on_commitment_id,
      quote, reason, status, superseded_by, created_at)`, `UNIQUE (user_id,
      commitment_id, dep_key)`. `dep_key` is a text discriminator (`fact:5`,
      `commitment:42`, `none`) rather than a multi-column UNIQUE, because SQLite treats
      NULLs as distinct and a NULL-bearing UNIQUE enforces nothing — that constraint is
      rule 3 for this table, so it must actually hold. Same migration adds
      `situation_doc (id, user_id, body, body_hash, created_at)`.
      **The checkbox was wrong and is corrected here** (pipeline-audit §3a): nothing of
      A1 existed when it was ticked. What landed instead, and supersedes the dependency
      half of this item, is migration **0032** `claim_event` + `claim_dependency` — goal
      3's table generalized past commitment to any subject table, per
      tasks/pipeline-redesign-2026-08-21.md §3.3. The `situation_doc` half landed as
      migration **0033**, its own migration so the state doc did not have to wait on the
      change ledger.
- [~] A2. **`logic_check` re-judgment** — landed differently, see the 2026-08-24 dependency note: no partial index and no `superseded_by`, because giving the table history needs a migration that re-arms the sidecar. The verdict is upserted and the old one recorded in `claim_event` (`relevance_rejudged`), and re-queueing is keyed on dependency row state rather than on a second verdict row.
- [~] A2 (original design, superseded — kept for the reasoning). **`logic_check` re-judgment** — the `UNIQUE (user_id, commitment_id)` table
      constraint blocks a second verdict, so 0031 rebuilds the table (create/copy/drop/
      rename) with a *partial* unique index `WHERE status != 'superseded'` and adds
      `superseded_by`. Verdicts supersede, they never UPDATE — house style, and the
      history is what makes a re-judgment auditable rather than a row that changed its
      mind silently.
- [x] A3. **`backglass/situation.py`** — `render(conn, settings, day) -> str`: who and
      where (identity/housing/enrolment facts), the current phase, the active fronts,
      and **what recently changed** (facts superseded or retracted in the window, with
      both values). Every line carries `[fact N]` / `[commitment N]`, so rule 1 holds
      inside the doc and a dependency can point at a claim that has provenance.
      `save()` writes a new version only when `body_hash` differs — rule 3, and the
      version list is the evolution the owner asked to see.
- [x] A4. **The relevance pass records dependencies, not just drops.** Extend
      `RelevanceVerdict` with `depends_on: list[int]` (fact ids) and require it on
      *every* verdict including `keep` — same call, same batch, richer output. The
      existing `cites_fact` stays as the drop citation; it is the degenerate
      single-dependency case. Same three guards: ids intersected with what was sent,
      facts intersected with the active set, quote checked against the source item.
      `check-relevance.md` → version 2, fixtures for the new field, no live API.
- [x] A5. **The checker reads the situation.** `render()` feeds the relevance prompt
      alongside the raw addressable facts — the model currently sees atomic facts and
      not the shape of the week. Input cost is nearly free here (2026-08-07 audit), so
      this is not pre-optimised away.
- [x] A6. **Surfaces** — `uv run backglass situation` (and `--json`), plus the doc and
      its version diff on the Memory page. Named `situation`, not `state`: `backglass
      state` already means installation ground truth and the collision would be cruel.

landed (2026-08-24, branch `worktree-situation-doc`): migration **0033** (`situation_doc`,
body + hash + created_at, no dedup constraint — the hash gate lives in `save()` because
"same as the previous version" is not "same as any version"); `backglass/situation.py`
with four sections — WHO (every active fact, addressable, uncapped), WHAT CHANGED (60-day
window over `superseded_by`, both values, retraction named as itself), WHAT IS HAPPENING
THIS WEEK (`context._situation`, *called* not reimplemented), WHAT THE OPEN OBLIGATIONS
REST ON (`claim_dependency`, honest-empty until a writer exists); `save`/`latest`/
`versions`/`changes`/`refresh`; `backglass situation [--refresh] [--versions] [--json]`;
the Situation panel and its version diffs on /memory; the sync epilogue refreshes after
the logic check and the replan and before the vault; two `backglass state` claims
(`situation_versions`, and `situation_current` — whether the newest stored version still
matches what renders now, the same shape as `deployed.matches_source`, because a stored
version is not evidence that it still describes anything); and A5 — `check-relevance@2`
carries the doc minus its facts section, since the pass already ships the facts under the
header its citations are validated against. 23 tests.

The Situation panel is **closed by default**, decided after reading it on the live ledger
rather than in advance: its WHO section is 66 facts and every one of them is already an
editable row further down the same page, so open it put 39 KB of duplicate above the thing
the page is for. The banner states what it holds while closed, and `backglass situation`
prints the document in full.

Three decisions worth reading before changing any of it:

- **The body carries no as-of date.** Everything in it is day-relative, and a date in the
  body is a hash that changes at every midnight for no change in the ledger — the version
  list would become a log of how often the job ran, which `save` exists to prevent. The
  day is `created_at`. Mutation-checked: putting the header back turns the test red.
- **`_rests_on` renders empty on every real ledger today, and that is correct.** No pass
  writes dependencies yet (A4 is unbuilt), and `claim_events.py`'s own rule is that an
  empty result means *unknown*, never *confirmed independent*. The section shipped ahead
  of its writer deliberately: it is where a wrong dependency has to become visible, and a
  section added after the writer is one nobody reads the first time it matters.
- **A4 was deliberately not faked.** Recording `depends_on_none` from a v1 `keep` that
  merely did not cite would be an overclaim — the prompt never asked what the obligation
  rests on — and a dependency recorded at drop time never appears in a section that joins
  *open* commitments. Dependencies arrive with relevance v2, as their own increment.

Verified against a copy of the live 60 MB ledger, not fixtures: 0033 applied clean; the
doc renders 109 lines / 18,937 chars over 66 active facts and 14 fact changes; the judge's
half (WHO omitted) is 6,271 chars. A second `refresh` the same day wrote nothing (rule 3
on real data). The next day's render *did* differ and legitimately — one plan left the
7-day window and the due-soon count moved 27 → 29 — which is exactly the churn/change line
the hash gate is supposed to sit on.

Two things the probe surfaced that are **not** fixed here, stated rather than buried:

1. The week section leads with the five stalest overdue rows ("Clean fishtank", due
   2026-01-06) because `context._situation` orders by due date ascending — pipeline-audit
   §4a, a known defect in `context.py`, which is dirty with another session's work in the
   shared checkout. Fixing it there is the cheap correct fix and it is not this change's
   to make.
2. `fact` 25 → 71 is a supersession whose old and new values are **identical** — the
   knowledge base wrote a new row for no change, which increment 2's "same-value
   re-emission is a no-write" says must not happen. The doc is what made it visible.

### Increment B — invalidation, dry run only. Nothing writes.

- [~] B1. **`logic.py` gains `_dependency_no_longer_holds`** — not built as a rule, and deliberately: `facts.remember` already breaks dependents through `claim_events.invalidate_fact` at the moment the fact moves, so a deterministic rule re-deriving the same thing on a later pass would be a second answer to a settled question. The invalidation feeds the relevance re-queue instead.
- [~] B1 (original design, superseded — kept for the reasoning). **`logic.py` gains `_dependency_no_longer_holds`** — an open commitment whose
      active dependency names a fact that is now `superseded` or `retracted`. This is a
      positive contradiction and belongs in that file: the ledger holds the row that
      contradicts the row being closed, and the rule can print both values.
- [x] B2. **Targeted re-judgment** — a broken dependency supersedes its `logic_check`
      verdict and re-queues that commitment, and *only* that commitment. Keyed on
      (commitment, fact version), so an unchanged ledger re-judges nothing and writes
      nothing on the second pass. Rule 3 asserted on real data, not fixtures.
- [x] B3. **The would-drop list, printed and classified row by row.** Three read-only
      runs against the live ledger, zero writes, every row read and classified before
      anything is believed. The 2026-08-20 lesson is about this exact shape of mechanism
      coming within one clean run of retracting sixteen live classes, and it says
      explicitly that a passing suite is not evidence — both defects passed all 32 tests.

### Increment C — enable, after the owner has read the list.

- [x] C1. **Drop or ask, asymmetric by consequence.** At or above the drop threshold the
      commitment is tombstoned citing the dependency and the fact that superseded it;
      below it, one question, nothing dropped. Inherited from `relevance.py` unchanged,
      because a wrong keep costs a click and a wrong drop is silent.
- [x] C2. **Backfill** — ~226 open commitments through the dependency pass, several syncs'
      work at `PER_RUN = 75`. A pass of this size measured ~$6.50 on 2026-08-19; the
      spend cap (rule 7) is a hard stop and this must degrade into it, not around it.
- [x] C3. Wire into `sync.py` behind the same gate the relevance pass uses.

### Constraints specific to this goal

- Migration 0031 re-arms the frozen-sidecar crash — the rebuild step goes in the commit
  message, per the constraints section above.
- `commitment` → `commitment` dependencies are recorded but do **not** gain a planner
  ordering behaviour in this goal. Recording is A; acting on it is a later increment, and
  conflating them would put an unreviewed scheduling change inside a deletion change.
- The dirty tree holds uncommitted retraction/scrub work. This goal does not touch those
  files and must not be tangled into their commits.

## Goal 4 (2026-08-20): the assignment is read, not just its title

The owner: *"be able to look at assignment and dynamically decide time needed and
materials needed."*

### What the probe found, before any design

- 159 `canvas:ics` assignments are in the ledger. **120 of the 143 open ones carry an
  identical `type_default:30`** — "Take PSY101 Exam 4 (Ch. 13-15) via LockDown Browser"
  and "Complete LearningCurve 14a" are the same half hour to this system. Every capacity
  number over coursework is arithmetic over one constant nobody chose.
- The feed carries what would answer it and the connector throws it away. 169 VEVENTs,
  **74 with a real DESCRIPTION** — instructions, tool names, links, page counts — and
  runtimes in the SUMMARY itself (`1-1-1 - Tech in the 21st Century (12:35)`).
  `canvas_ics._to_item` stores `"{course}: {title} is due {due}."` and drops DESCRIPTION,
  URL and everything else.
- **The obvious fix is barred by immutability, and it must be said before anyone tries
  it.** `ledger.upsert_source_item` treats a differing `content_hash` on a stored
  `external_id` as a conflict: it records it and skips. Putting DESCRIPTION into
  `body_text` therefore rewrites the hash of all 159 stored assignments, produces 159
  `content changed for an immutable source_item` run errors, a non-zero exit, and **not
  one byte of new data** — the rows keep their terse bodies forever. `content_hash`
  covers (author, title, body_text, occurred_at) and *not* `raw_json`, so the raw record
  can be widened for future items at zero cost, but the backlog needs somewhere else.
- **A live bug, found by comparing the feed to the ledger rather than by a test.** Five
  CIS236 assignments moved upstream — 1-1-1 through 1-1-4 from 2026-08-23 to 08-25, the
  Team Charter from 08-31 to 09-04 — and the ledger still holds the old dates. Same
  cause: the re-read produced a conflict, the conflict was logged, nothing propagated. A
  deadline extension is invisible to this system today, and one assignment in the ledger
  is no longer in the feed at all.

**Stated assumption, so it is not invisible:** "look at the assignment" is read as *the
assignment as it stands upstream now*, not the frozen sentence captured the day it was
first seen. That is what forces a typed `assignment` record with a `last_seen_at` beside
the immutable `source_item` — the ledger stays primary for the obligation, and the record
holds the thing that legitimately changes. Additive per CLAUDE.md: if the table vanished,
every existing surface is still correct, just back to a flat 30 minutes.

### Increment A — the coursework record and what the text states outright. No model.

- [x] A1. **Migration 0031** — `assignment` (source, external_id, source_item_id, course,
      title, due_at, url, description, description_hash, effort_minutes, effort_basis,
      effort_quote, sessions, analyzed_hash, first_seen_at, last_seen_at, UNIQUE
      (user_id, source, external_id)) and `assignment_material` (assignment_id, kind,
      name, detail, quote, basis, UNIQUE (user_id, assignment_id, kind, name), every
      column in the constraint NOT NULL — SQLite counts NULLs as distinct and a
      NULL-bearing UNIQUE enforces nothing, which is goal 3's lesson and rule 3 for this
      table). Re-arms the frozen-sidecar rebuild; note it in the commit. Goal 3's planned
      0031 renumbers to 0032.
- [x] A2. **The connector parses the whole VEVENT** — `CanvasIcsConnector` keeps the
      parsed assignments on itself (`self.assignments`) as it emits items, the same seam
      `seen_chats` / `excluded_by_rule` / `failed_calendars` already use: a connector
      emits SourceItems and nothing else, and sync reads the attribute afterwards. One
      fetch, one truth. DESCRIPTION and URL also go into `raw_json`, which is outside
      `content_hash` and so costs no conflict — future items stop being lossy even where
      the backlog cannot be repaired.
- [x] A3. **`backglass/coursework.py`** — `upsert()` writes an `assignment` row per feed
      entry, linked to its `source_item` by external_id, updating description/due/url in
      place and touching `last_seen_at`. Rule 3: an unchanged feed writes nothing, and
      `description_hash` is what decides "changed", not the fetch.
- [x] A4. **Effort from stated numbers.** Deterministic, each with its own basis and the
      quote it read: a video runtime in the title (`(12:35)`) plus a fixed overhead for
      the questions that follow it; a word or page count; a question count; a chapter
      range (`Ch. 13-15`) times a per-chapter rate. Where nothing is stated, an ASU-shaped
      coursework type (LearningCurve, exam, quiz, lab report, discussion, milestone) from
      a configurable table — `coursework_defaults`, beside `estimate_defaults`, so the
      numbers are configuration rather than taste buried in a regex.
- [x] A5. **Materials from the same text.** Links out of the DESCRIPTION, a tool lexicon
      (LockDown Browser, WeVideo, Tableau, Excel/Solver, Achieve), a reading when a
      chapter range is named. Every row carries the quote it came from — rule 1 holds
      inside this table too, because a material with no evidence is a guess the owner
      would have to check by hand, which is the work this is supposed to remove.
- [x] A6. **Into the commitment, respecting stickiness.** `estimate_source` gains
      `analyzed`, and the ladder is manual > extracted > analyzed > type_default: a number
      the source text stated outranks one this pass derived, and a number the owner chose
      outranks both. `estimates.backfill` must not re-derive an `analyzed` row.
- [x] A7. **Surfaces** — `uv run backglass coursework` (and `--json`), assignments with
      their effort, basis and materials.

landed (increment A, 2026-08-20, uncommitted — the tree also holds unrelated
retraction/scrub work and these files overlap it):
`0031_assignment.sql` (`assignment` + `assignment_material`, `last_changed_at` rather than
`last_seen_at` so rule 3 holds on the timestamps too); `canvas_ics.ParsedAssignment` +
`_parse`/`_item_for`, description and URL into `raw_json` where `content_hash` cannot see
them; `backglass/coursework.py` (stated runtime/words/pages/questions/chapters, the
`coursework_defaults` type table, a tool lexicon and the description's own links as
materials, every row carrying its quote); `estimate_source='analyzed'` and the
manual > extracted > analyzed > type_default ladder; `assignment` added to iMessage
`DEPENDENTS`; `backglass coursework [--refresh] [--dry-run] [--json]`; wired into
`sync._ingest` on the `getattr` attribute seam and `apply_estimates` after extraction.
26 tests.

Read row by row against a copy of the live ledger, which is where four defects were
caught that the suite was happy with: "Exam 4 (Ch. 13, 14, and 15)" read as a two-chapter
range (a list and a range are the same fact, so both expand to a set now); the largest
CIS 236 milestone estimated from the *first* size its 12,000-character description
mentions rather than the largest; "Excuse Note Submission Link" classified as a 120-minute
exam and "Excused Absence Requests" as a 90-minute lab, both read off a description
instead of a title — so `classify` asks the title alone first and a `form` type leads the
table; and "T - Team Planning" made an exam by the word "final" in its prose.

Live result on the copy: 158 assignments recorded, 92 materials, 133 commitment estimates
moved. Four exams 45m → 120m citing their chapters, five team milestones 30–60m → 138–344m
citing their page limits, two absence forms 30–45m → 15m. Second pass wrote nothing.

### Increment B — upstream drift, which is the bug already on the board

- [x] B1. A feed due date that differs from the commitment's moves the commitment, with
      the old value and the feed's own read time in the note, plus a `deadline-moved`
      notification through notify's dedup. This is positive contradiction — the upstream
      record disagrees with ours — not silence, so it belongs with `logic.py`'s rules.
- [x] B2. An assignment in the ledger and no longer in the feed is `retraction`'s shape,
      not this one's: the ICS feed is a windowed complete read and that module already
      knows how to certify one. Recorded here, deliberately not acted on in this goal.

### Increment C — the planner may not be handed honest numbers without this

- [x] C1. **Clamp.** `select()` drops any candidate whose minutes exceed the remaining
      capacity, so an honest 180-minute milestone would silently never be scheduled —
      strictly worse than the flat 30. A block clamps at `max_block_minutes`, the
      commitment stays open, and the remainder is tomorrow's rollover.
- [x] C2. **Session-aware done.** Increment 7 made `set_block_outcome('done')` resolve the
      commitment behind the block. With clamping, marking one 90-minute session done would
      close a three-session assignment. A clamped block records progress; only the last
      one resolves, and "worked on it" is an outcome.
- [x] C3. Materials render on the block, so the owner reads what to open before starting.

landed (increment C, 2026-08-20, uncommitted with A): `Candidate.done_minutes`,
`remaining`, `divisible` and `sitting()`; `select`/`propose` place a sitting and
`_block_title` says when a block is only part of the work — including the protected
slot, which was already truncating a long obligation to its fixed 90 minutes and saying
nothing about it. `actions._work_is_finished` gates the increment-7 resolve on the
sittings adding up to the estimate, so single-sitting work still closes on the click and
a four-session milestone no longer dies on the first one. `dashboard_today.sql` carries a
`needs` column and the Today row renders it. 8 tests.

**The discriminator is evidence, not size,** and it was chosen after an existing test
caught the first version: a blanket clamp cut "Move-in: Willow Hall 502" — three hours of
one thing — into 90-minute pieces. Only an `analyzed` estimate is divisible, because that
is one `coursework` read off a deliverable measured in pages, chapters or runtime.
Everything else is planned whole or not at all.

Both guards were mutation-checked rather than trusted: replacing `sitting()` with
`remaining` must turn the clamp test red, and removing the `divisible` branch must turn
the move-in test red. The first version of the clamp test passed under *both* mutations —
the protected slot was doing the truncation, not the clamp — which is precisely the
worthless-test shape the 2026-08-19 lesson describes, and it was rewritten to put
something else in the protected slot first.

Live proof on the ledger copy, 2026-12-07 (the day before Exam 4): the plan now holds
`Take PSY101 Exam 4 (Ch. 13-15) via LockDown Browser (90m of 120m left)` and two
`(90m of 344m left)` CIS 236 milestone sittings. Before this the same day gave that exam
45 minutes and the milestones 30.

### Increment D — the model reads what the text does not state

- [~] D1. **Not building this — measured 2026-08-25, see the note at the end.** `assignment-effort@1`, shaped exactly like `check-relevance`: bounded slice per
      sync behind the spend cap, judged once per (assignment, description_hash) so an
      edited assignment is re-judged and an unchanged one is never re-paid for, every
      material quote verified against the description in code, `--dry-run` first and the
      would-change list printed and read row by row before a single write. The 2026-08-20
      lesson is that a passing suite is not evidence for a mechanism that rewrites what
      the planner consumes.

---

## Classes page + Canvas coursework archive (2026-08-21, owner request)

The ask: read every Canvas course, download and organize the resources, put any schedule
on the calendar — then "continue to look at other classes and schedule and syllabi and
create a page on backglass for classes".

Done before this section was written:

- [x] Read all 8 Fall-C Canvas shells through the live Safari session (Canvas's own API,
      GET only). Files tab is 403 on 7 of 8 shells, so files came through `content_exports`
      where allowed and per-file `GET /files/:id` otherwise.
- [x] `~/Documents/ASU Fall 2026/` — 45 documents by course, each course's PDFs/DOCX
      extracted to `_text/`, Canvas-only content (syllabus page, module outline, every
      page) to `_canvas-text/`, `README.md` naming the gaps.
- [x] `_calendar/*.ics` — 28 events read out of the syllabi with the quoted line behind
      each, then written into a new Apple Calendar "ASU Fall 2026". Idempotent: the
      second run created 0 and skipped 28.
- [x] BIO 181's final exam hour recovered from the registrar's Fall 2026 matrix
      (Wed 12/9 9:50–11:40) rather than left TBD.

Remaining, in this order (INBOX last, because the scheduler runs this checkout and an
ingest of a folder still being written is an ingest of a half-built folder):

- [x] `backglass/courses.py` — one reader, ledger-only: meetings from `calendar:asu`
      (room, instructor, weekday pattern), coursework from `assignment` + its materials,
      exams from the `ASU Fall 2026` calendar rows once `apple_calendar` ingests them,
      documents from `files` rows under each course's folder. No new table: every column
      this needs already exists, and a migration would be live on the scheduler's clock
      within 30 minutes (2026-08-21 lesson).
- [x] `/classes` page: one card per course, `id="panel-…"` markers, keyline tiles.
      Route in `backglass/web/routes/classes.py`, template `classes.html`, nav entry.
- [x] Tests: 12 in tests/test_courses.py + 4 page tests. Full suite green (2,348).
- [x] `INBOX_FOLDER_PATH` → the archive, with `_text/`, `_canvas-text/` and `_calendar/`
      moved out of the drop root first: the files connector reads `.txt`, so leaving the
      derivatives inside would ingest every syllabus twice and pay extraction for both.
- [x] Rebuild the desktop sidecar, or say plainly that it is behind.

Known gaps, not silently dropped: CIS 236's syllabus and schedule are Google Docs shared
with the ASU account, and Safari is signed into the personal one — they need an ASU login
to read. Two Canvas files (PSY 101 Exam 1 study guide, BIO 181 Lab Week 3 activity) return
HTTP 500 from Canvas itself.

Landed 2026-08-21T17:43Z, run 521: 29 files ingested, triage kept 4 (both CHM 113 lecture
documents, the recitation activity, the HON 171 syllabus) and dropped 25 readings and
worksheets, extraction wrote **12 commitments** — including HON 171's midterm essay
(10/4) and final paper (12/6), which existed nowhere in the ledger before. `MAX_BYTES` in
connectors/files.py went 512 KB → 4 MB in the same pass: seven of the archive's documents
were over the old ceiling and every one was a document, not a dump.

Two things the page states rather than hides: all-day calendar rows never enter the ledger
(apple_calendar drops them, docs/07), and a timed exam only arrives inside the connector's
21-day horizon. The durable record of a syllabus date is the extracted commitment, which
is what the course cards list.

## Stutter audit + motion pass — 2026-08-21

**Measured, not guessed.** Every dashboard route timed against the live 56 MB ledger
(`data/backglass.db`, 317 open commitments), warm and cold:

| route | before |
|---|---|
| `/` | 6.48 s, 1.37 MB HTML |
| `/schedule` `/goals` `/people` `/roadmaps` `/memory` `/chats` `/brief` `/decisions` `/classes` | 3.21–3.58 s each |
| `/static/dashboard.css` (131 KB) | 0.006 s |

Static serving is fine. The floor is Python CPU, not SQLite: cProfile over
`panels.sidebar` attributes 3.28 s as 50 135 `difflib` ratios + 24 090 pure-Python
cosine comparisons (18.5 M generator iterations, three times over), against 0.089 s
of `sqlite3.execute`. 97 % of a page load is duplicate detection.

Chain: `sidebar_state` middleware (every full-page GET) → `panels.sidebar` →
`board_panel` → `dedup.suspects` → `entities.similar` (O(n²) difflib) +
`search.duplicate_pairs` (O(n²) cosine). `dedup.suspects`'s own docstring says
"n is a personal ledger's open set, double digits … measured in milliseconds" —
falsified: n is 317, and `_semantic_pairs` was added on top after that claim was written.

`/` costs double because `board_panel` runs twice per load: once in the middleware's
`sidebar` (panels.py:733) and again in `everything` (panels.py:895).

### Plan

1. `board_panel(..., include_suspects: bool)` — the sidebar reads `len(board.rows)`
   only; `meta["suspects"]` has exactly one consumer, `_board.html`. Skip it in the
   middleware. Removes the whole cost from nine routes.
2. `entities.similar` — return `jaccard` without building a `SequenceMatcher` when
   difflib's own upper bound (`2·min(la,lb)/(la+lb)`, then `quick_ratio`) cannot beat
   it. Exact, not approximate: the function returns `max(jaccard, sequence)`, so an
   upper bound at or below `jaccard` decides the result.
3. `search.duplicate_pairs` — unit-normalise each vector once instead of recomputing
   both norms inside every pair, and dot through `map(operator.mul, …)`. Drops 2/3 of
   the arithmetic. No new dependency (numpy is not installed and is not worth adding
   for one loop).
4. Move the remaining blocking sidebar work off the event loop — it is sync CPU inside
   an `async def` middleware, so it serialises every other request behind it.
5. `/`'s 1.37 MB of HTML: attribute and cap server-side.
6. Motion pass — see §Motion substitution below.

### Motion substitution (stated assumption)

Framer Motion is React-only. This app is server-rendered Jinja + HTMX with three closed
decisions against it (no frontend framework, no npm, no build step — docs/10 §Web layer).
Assuming the intent was the motion *quality* rather than the React dependency: vendor
**Motion** (motion.dev — Framer Motion's own vanilla engine, same authors) as a single
checked-in file with its hash in `static/VENDOR.md`, exactly the htmx precedent.

The CSS §Motion layer stays as the base — `@starting-style` keeps driving arrival, so
`test_every_swap_target_arrives` keeps meaning what it means. Motion is used only for
what CSS cannot express: stagger on list arrival, and FLIP on rows that move or leave.
Every value comes from the existing tokens (160 ms, `--ease-out`, 4 px, 0.97), and
`prefers-reduced-motion` is honoured. No springs, no lift — design-system.md §9.

**Written back into the docs and the knowledge base (same day).** docs/07 gained the
Canvas archive read (and the paths that do not work), the drop folder in practice, and the
two ways a calendar write fails to reach the ledger; docs/06 gained the Classes page;
docs/03 records the coursework tables and why there is no course table; docs/13 warns about
derived text in a drop root. Four facts written to the ledger's knowledge base —
`fall-2026-course-load`, `fall-2026-exam-pattern`, `coursework-archive`, and a correction
to `meeting-days`, which extraction had set from the wrong HON 171 section (9:00–10:15 for
section #85305 instead of the owner's 10:30–11:45). That one was live in `owner_context`,
so it was reaching every model call.


### Result — measured the same way as the audit

Same routes, same 56 MB ledger, same machine, warm and cold:

| route | before | after | |
|---|---|---|---|
| `/` | 6.48 s · 1.37 MB | **0.19 s · 0.33 MB** | 34× faster, 4× smaller |
| `/schedule` | 3.23 s | **0.11 s** | |
| `/goals` | 3.21 s | **0.10 s** | |
| `/people` | 3.26 s | **0.15 s** | |
| `/roadmaps` | 3.37 s | **0.11 s** | |
| `/memory` | 3.58 s | **0.11 s** | |
| `/chats` | 3.25 s | **0.11 s** | |
| `/brief` | 3.40 s | **0.10 s** | |
| `/decisions` | 3.23 s | **0.14 s** | |
| `/classes` | 3.33 s | **0.13 s** | |

The dashboard build itself (`panels.everything`) went 2.62 s → 0.127 s. htmx-bound
controls on `/` went 3 464 → 913, which is the half of the stutter a stopwatch on the
server never sees: every one of them is an element the webview binds before the page
can be touched.

Done, all verified against the live ledger:

1. `board_panel(include_suspects=False)` — the sidebar middleware no longer runs the
   duplicate pass to read one number off the panel.
2. `entities.similar` short-circuits on difflib's own upper bounds. Exact; it fires
   rarely on natural-language pairs, so most of the win came from (3) and (4).
3. `search.duplicate_pairs` unit-normalises once and dots through `map(mul, …)`, and
   memoises on a digest of the vectors it read. 4.8 s → 0.31 s → 0 s on a second read.
4. `dedup.suspects` memoises wording scores by id, invalidated by text — a read after a
   sync costs the pairs that changed, not all 50 135 of them.
5. The sidebar's blocking work runs in a threadpool, so a slow panel can no longer
   serialise the stylesheet, the font and every in-flight write behind it.
6. `serve()` warms the pass on a daemon thread at launch, so the first page the owner
   opens is not the one that pays for it: 3.2 s → 0.52 s on the first-ever request.
7. Suspects capped at 20 of 799, review queue at 40 of 340, both with the count said out
   loud above and below the rows. 657 KB and 461 KB of collapsed folds left the page.

### Motion — done

`motion/mini` 13.1.1 vendored (12 KB, hash in `static/VENDOR.md`), and `static/motion.js`
spends it on the two things CSS cannot say: a stagger on rows that arrive, and a FLIP on
rows that move because another row left. Verified in Safari against the demo ledger, not
by reading the code: resolving a commitment logged `flip c1 246.6` with `travel 4`,
`step 0.024`, `dur 0.16` — every value read from `design/tokens.css`.

A third handler, flashing the acted-on row after a write, was written, instrumented,
found never to fire (htmx detaches the element before `afterRequest`) and deleted. The
note in its place says where that feedback actually comes from.

Five tests added in `tests/test_motion.py`: the script invents no duration, distance or
curve; it stands down for reduced motion; it animates transform and opacity only; the
vendored engine matches its recorded hash; base.html loads it. Full suite: 2355 passed.

### Second pass — 2026-08-21, same day

**The engine came out.** `motion.js` now calls `Element.animate` directly. Motion's
`mini` build is a wrapper over exactly that call, so the 12 KB bought API sugar and
nothing else — and docs/10 §Web layer's "no build step, no npm, no bundler" is satisfied
more completely by a browser API than by a vendored copy of a wrapper around one. Gone
with it: `static/motion.min.js`, its `VENDOR.md` entry, its hash, and the `type="module"`
on the script tag.

Verified in Safari, not inferred: resolving a commitment logged three FLIPs
(`c4 234.3`, `c2 346.6`, `c1 507.6`) with `dur 160 · travel 4 · step 24 ·
cubic-bezier(0.23, 1, 0.32, 1)` — the easing token now reaches the animation as the
string tokens.css writes, because WAAPI takes CSS easing verbatim and there is no array
to convert it into.

**FLIP now measures only what is on screen.** `getBoundingClientRect` forces layout, and
it was being forced for up to sixty rows including ones scrolled thousands of pixels
away. On the board that is six rows measured instead of sixty, for an identical result.

**`content-visibility: auto` on `.row.card` and `.rrow`** — the board's 187 cards and the
review queue's 40 rows are laid out and painted on every load for the eight rows that fit
on a screen. `contain-intrinsic-size: auto 100px` so the scrollbar stays honest. Scoped
to those two selectors deliberately: `.row` alone would catch the schedule and sources
panels, which are a screen long and would pay containment for nothing.

Stated plainly, because it is the one claim here without a number behind it: this is a
standard rendering optimisation and it was verified to cause **no visual regression** by
an A/B on one origin — it was **not** measured as a frame-rate improvement on this
machine, because the CSP blocks the automation's `getComputedStyle` and there is no
devtools timeline in reach.

The first A/B run said the rule blanked the Today panel. It did not: the two builds were
served on different ports, panel folds persist in `localStorage`, and `localStorage` is
keyed by origin — one origin had Today collapsed and the other did not. The `+` against
the `−` in the panel header is what gave it away. Re-run on a single origin, the two
renders are identical.

### Not done, and why

`hx-boost` on the sidebar would turn every page change from a full document load into a
body swap — no CSS re-parse, no re-binding the whole page. It is the largest remaining
navigation win. Left alone because several pages carry their own `{% block scripts %}`
with document-level listeners, and boosting without reworking those registers them again
on every navigation. Worth doing deliberately, not as a rider on a performance pass.

### Third pass — 2026-08-21, motion audited against §9 itself

Re-read `design/design-system.md` §9 before adding anything, and it had already refused
most of what was left: exits, page cross-fades, selection, focus rings, hover reveals,
progress bars — each with its reasoning. That saturation is the finding. Two things
remained that §9 permits, and one thing I had already shipped that it forbids.

**Removed: the stagger.** §9: *"No stagger anywhere. Panels arrive together."* The
version I built was narrower than the one that sentence refuses — rows inside one
swapped panel, capped at twelve, never on page load — and it failed on §9's own terms
twice over. Twelve rows at 24ms put the last one 288ms behind the first, on the Resolve
and Done writes the frequency table assigns to the 90ms-or-nothing tier; and the step
needed a sixth duration token, in a vocabulary whose argument for having five is that
you pay per duration. `--motion-stagger` is gone from tokens.css.
`test_the_script_does_not_stagger` now holds the line. If you want it back it is a
one-line §9 ruling and I will put it back.

**Corrected: the FLIP's curve.** §9 defines two curves by role — `--ease-out` for
anything entering, `--ease-in-out` for anything *moving on screen*. Nothing in the
product had ever used the second one; the split was declared in advance and the FLIP is
its first occupant. It had gone in on `--ease-out` because that was the curve everything
else used.

**Added: the panel-opening reveal.** `<details>` reveals content by flipping a boolean,
and a revealed element is not a newly inserted one, so `@starting-style` cannot see it —
the one arrival in the product CSS cannot select. It gets §9 mechanism 1 exactly: fade
up four pixels over `--motion` on `--ease-out`. Opening only; nothing leaves. The
`<summary>` is excluded because it never went anywhere.

Both recorded in §9 as entries 6 and 7, and the stagger refusal keeps its rule with a
weighed-and-reverted note underneath, in the doc's own idiom.

### Two bugs the probe found, one of them a day old

**The FLIP had never actually animated anything.** `htmx:afterSwap` reports
`event.detail.target` as the element it replaced, and for an `outerHTML` swap — which is
every write on this dashboard — that element is already detached. `getBoundingClientRect`
on a detached node returns zeros, so the delta came out as each row's *absolute offset*
(245, 346, 507 — successive card tops) instead of the distance it moved, and
`el.animate` was then handed a node that would never paint again. Nothing threw. Every
log line looked right. Yesterday's verification read those numbers as deltas and they
were not.

Fixed by querying the live `document`; the `before` map is keyed by commitment id, so
that is both correct and simpler. Now measures `dy=109.0` — one card height, the real
distance — with `live=true`. `test_the_flip_measures_the_live_document` guards it.

**WebKit fires `toggle` on an inserted `<details open>`.** Not just on one that opens.
So every swap announced every panel it replaced as freshly opened, and the new fold
reveal fired on top of the `@starting-style` arrival already animating them — two
animations on the most frequent action in the product. The first fix was a time window;
the echo arrived 2ms after one swap and 178ms after the next, so that was a coin toss.
Replaced with remembered state: a panel that was open and is still open did not open,
whoever fired the event.

Verified in Safari against a throwaway copy of the demo ledger: FLIP `dy=109.0
live=true cubic-bezier(0.77, 0, 0.175, 1)`; fold `cubic-bezier(0.23, 1, 0.32, 1) 160 4`
on a user open; nothing on close; nothing on a swap. Full suite 2374.

---

# The day is planned around the day: routines shift, work stops (2026-08-21)

Owner's ruling, this session: *"everything should be planned around my schedule and
fixed events; breakfast, lunch, dinner and gym should be planned around this, along
with some amount of relaxation time and study time and homework time."*

## What is actually broken

The live plan for Monday 2026-08-24 states it:

```
  12:20pm–1:10pm  CHM 113 [fixed]
  12:30pm–1:15pm  Lunch [routine]
  ...
  8:00pm–9:30pm   Call the phone-only hospices ... [protected]
  9:30pm–11:30pm  RSVP + attend Arizona AI meetup
```

1. **Routines are rigid clock times.** `parse_routines` (config.py:83) emits a span at
   `HH:MM` and `routine_events` (capacity.py:454) stamps it on the day whatever else is
   there. Lunch sits inside a chemistry class. `_distinct` does not merge them — same
   minute, different titles — and the span merge in `compute` charges the overlap once,
   so nothing is *miscounted*; the plan is simply wrong about when the owner eats.
2. **Nothing protects the evening.** `WORKING_WINDOW=07:00-23:59`, so the planner is
   entitled to every minute up to midnight and takes them.
3. **Study is unrepresented.** Homework already exists — coursework commitments are
   scheduled by name — but a day with no coursework selected gets no study time at all.

## One mechanism, not three

Routines are already a validated, day-scoped, buffer-exempt, capacity-subtracting,
rendered concept. Relaxation is a routine. The evening stop is the working window.
Homework is what the planner already does. Only two things get built.

- [x] 1. **Flexible routines** (`config.py`, `capacity.py`)
      A routine names a *preferred* start, not a decreed one. When its span collides
      with a calendar event or a confirmed engagement, it moves to the nearest free gap
      that fits, searching outward from the preferred start, earlier before later on a
      tie (deterministic — 0028's `inputs_fingerprint` and rule 3 both depend on the
      same inputs producing the same placement byte for byte).
      - Drift is bounded by `ROUTINE_MAX_SHIFT_MINUTES` (default 120). Lunch may become
        1:15pm; it may not become dinner.
      - **No-fit fallback, decided rather than emergent:** no gap within the bound →
        the routine keeps its preferred time and the plan carries a note
        (`lunch overlaps CHM 113 — no free 45m gap between 10:30 and 14:30`). A meal is
        never silently deleted and never silently moved out of the day.
      - **Pinned routines keep the old behaviour**, spelled `banner@16:00+240!@wed`.
        The docstring's own example is real: Wednesday volunteering at a stated hour is
        an obligation, not a preference, and must not drift. Flexible is the default
        because the owner ruled that way; the marker exists for the exception.
      - Placement lives in `day_events()` — after calendar and engagement events are
        resolved, since those are the skeleton it fits into, and before `compute` clips
        to the window. That is the one builder all three consumers read (capacity, the
        persisted plan, the schedule page), so they cannot disagree about lunch.
      - Routines do not shift each other: they are placed in preferred-start order and
        each placed routine joins the obstacle set, so breakfast cannot land on lunch.
      - `not_before` (a plan built at 4pm) must not shove lunch to 4:15. Placement runs
        against the unclipped day in `day_events`; `compute` clips afterwards, exactly
        as it does today. A routine whose window has passed stays where it was.

- [x] 2. **The evening is not capacity** (`.env`)
      `WORKING_WINDOW=07:00-21:00`, and `relax@21:00+120` joins ROUTINES. Work stops at
      nine; relaxation renders on the schedule and spends nothing, the same way
      breakfast at 07:30 does today under the old window. No ceiling machinery, no new
      concept — the hard stop *is* the window, which is the one number that already
      means "when may work be scheduled".
      Consequence to state plainly: the day loses ~180 minutes of capacity (1019 → 840
      window minutes), so more items land in overflow. That is the point of the ruling.
      The 9:30pm meetup block moves to overflow with it.

- [x] 3. **A study block on light days** (`plan/planner.py`)
      Owner's answer: study is not a second name for homework; it is what a day without
      homework should still contain. After `select`, if no selected candidate is
      coursework (`estimate_source = 'analyzed'` / course-linked), append one synthetic
      `Study` block of `STUDY_BLOCK_MINUTES` (default 90) into the capacity that is
      left, placed by the same `place` path as everything else — so P4 (min block), P5
      (no overlap) and P8 (nothing small in the protected block) hold for free.
      It is a block, not a commitment: no ledger row, no rollover, nothing to mark done
      but the block itself. A plan is allowed to say "read" without inventing an
      obligation the owner never made.

- [x] 4. **The owner's real routine set** (`.env`)
      Live `.env` has no `ROUTINES` line at all — everything above runs against the
      code default. Shipping the ruling means writing it down:
      `breakfast@07:30+30, lunch@12:30+45, gym@17:30+60, shower@18:35+25,
      dinner@19:15+45, relax@21:00+120`.

- [x] 5. **Tests** (`tests/test_capacity.py`, `tests/test_planner.py`)
      collision → shift to the nearest gap; tie broken earlier; no-fit → preferred time
      plus a note; pinned routine does not move; two runs over one frozen day produce
      the identical placement and zero writes (rule 3); both zones, UTC-7 and +05:30
      (the 2026-07-30 timezone lessons); a day-scoped routine still filters; a routine
      already outside the window still renders and still spends nothing; study block
      appears only when no coursework was selected.

## Assumptions stated, so they are not invisible

- "Relaxation 21:00–23:00, hard stop on work at 21:00" is read as *the working window
  ends at 21:00*. The owner's earlier ruling ("plan around my usual hours of 7 to 12",
  .env:9) is superseded for work hours only; 21:00–23:59 remains their day, it is
  simply not the planner's.
- "Nearest free gap" is bounded at two hours. Unbounded, a fully booked afternoon puts
  lunch at 4pm, which is not lunch.

## Process

Two Claude sessions are live in this checkout (preflight, four prior commit races on
record). Implementation happens in a worktree; only the merge touches the shared tree.

## What landed

`config.py` — `Routine.pinned` and a `!` marker in `_ROUTINE_RE`; `study_block_minutes`.
`plan/capacity.py` — `ROUTINE_MAX_SHIFT_MINUTES`, `FixedEvent.conflict`, placement in
`routine_events(..., around=)` via `_nearest_gap`/`_overlaps`/`_clashing_titles`, and
`day_events` deduping calendar + engagements *before* handing them to placement.
`plan/planner.py` — routine conflicts surfaced as notes, and the study block.
`.env` — window 07:00–21:00 both weekday and weekend, ROUTINES written down with
`relax@21:00+120`. UI: `_today.html`, `dashboard.css`, `_macros.html` legend.
Tests: `tests/test_routine_placement.py`, 20 of them. Suite 2394, green.

Live proof on the owner's own ledger, Monday 2026-08-24, the day of the complaint:

```
  12:20pm–1:10pm  CHM 113 [fixed]
  1:10pm–1:55pm   Lunch [routine]      ← was 12:30–1:15, inside the class
  ...
  9:00pm–11:00pm  Relax [routine]      ← and no work block after 9
```

## Deviations from the plan above

1. **The study block got its own `kind`, having first been built as `kind='work'`.**
   `rollover.open_blocks` rolls every pending work/protected/small block into tomorrow,
   so an hour of reading nobody did arrived the next day as an obligation the owner
   never made — and P11 would eventually have asked whether to drop a commitment that
   does not exist. Caught by `test_rolling_over_increments_the_count_on_both_rows`
   going from 1 to 2. `study` is excluded from rollover and included in
   `planned_minutes`; on the schedule it shares the routine register rather than
   claiming a sixth ink (design-system §2), which is honest — both are time the day
   holds rather than work it owes.
2. **Homework is read before the protected block takes it.** `propose` pops the first
   real piece of work off `scheduled` into the protected slot, which is exactly where a
   coursework item lands — so asking "did any homework get scheduled?" afterwards saw
   an empty list and gave a day full of homework a study block on top of it. Found by
   the test, not by reading.
3. `ROUTINE_MAX_SHIFT_MINUTES` is a module constant in `plan/capacity.py`;
   `study_block_minutes` is a `Settings` field, since turning study off is a preference
   and the drift bound is a property of the algorithm.
4. Tests live in `tests/test_routine_placement.py`, not `test_routines.py` — that name
   was already taken by the parser/clock tests. It was taken *destructively* first; see
   the 2026-08-21 entry in `tasks/lessons.md`.
5. Not done in a worktree, deliberately. The shared checkout carries ~40 files of
   uncommitted work (`staleness.py`, `courses.py`, modified `capacity.py`) that a
   worktree branched from HEAD would not have — the tests would not have imported.
   Commits are path-scoped instead (`git add <exact paths>`, never `-A`), which is what
   the four recorded races actually came from.

## Left for the owner

The desktop sidecar freezes templates, CSS and Python at build time: `backglass state`
reports `matches_source: False` with `_today.html`, `dashboard.css`, `_macros.html`,
`config.py`, `capacity.py`, `planner.py` stale. The CLI and the 05:45 job are already
live on this; the app needs a rebuild to show it.

## Second pass: the rules are in the doc, not only in the code (2026-08-21)

Owner: *"continue to add these rules and logic to daily planner."* Read as: codify the
behaviour as planner rules where every other planner rule lives, so it is a decision the
next reader inherits rather than a comment they have to find.

- [x] **docs/04 §1.9 "Routines, relaxation, and study"**, added after §1.8 rather than
      inserted mid-document: §1.5, §1.6 and §1.8 are cited by number from ~30 places in
      the code, and renumbering to make room would have broken all of them silently.
      Four new rules, continuing from P16:
      **P17** the configured hour is preferred, not fixed — nearest free gap, earlier
      wins ties, bounded; **P18** an unplaceable routine keeps its hour and the plan
      states what it overlaps; **P19** a pinned routine never moves and reports no
      conflict; **P20** a day whose plan holds no coursework gets one study block, never
      rolled, never a commitment. §1.2's capacity formula now names routines as part of
      the fixed picture, and §5 gained three acceptance criteria.
- [x] **Citations both ways** — `plan/capacity.py` and `plan/planner.py` docstrings name
      P17–P20 the way they already name P1–P10; `tests/test_routine_placement.py` names
      the section it proves; `.env.example` documents the preferred-hour rule, the `!`
      pin and `STUDY_BLOCK_MINUTES`.
- [x] **P18 made true on the surface the owner reads.** It was only half-implemented:
      the conflict was stated in the CLI's proposal notes, which are in-memory, and the
      brief rebuilds every line from the ledger — so a lunch with nowhere to go was
      announced to a terminal nobody opens at six in the morning. `brief/daily.
      plan_section` now recomputes `day_events` and carries any conflict as a line with
      provenance (rule 1). Recomputed rather than stored because `day_events` is
      deterministic over an unchanged day, which is the property 0028 already leans on.
      Suite 2395, green — 2394 plus exactly the one test added, checked as the
      2026-08-21 lesson now requires.

### Not done, deliberately

The dashboard's Today panel does not show the conflict; the brief and the CLI do. The
panel reads `plan_block` rows and the conflict is not a block, so putting it there is
plumbing (`panels.today_panel` would have to read the day's events too) rather than a
line of markup. Say the word and it is a small change; it is not silently missing.

---

# The day is empty and the checker is too polite (2026-08-24, owner)

The owner: *"add more logic and more home work time and coding time among other things"*.

## What the probe found, before any design

`2026-08-26` is a live proposed plan. Capacity says **215 minutes**; the day shipped with
**one 30-minute block**. Reading the planner against that day rather than against fixtures:

```
free slots:  07:00-07:30 30m · 08:00-09:00 60m · 11:55-12:20 25m
             13:55-14:45 50m · 16:55-17:30 35m · 20:00-21:00 60m   (215m, largest 60m)
candidates:  183      select -> scheduled=2  small=6  overflow=175
first pick:  90m, priority 4 (REST), due 2026-11-13
```

Two defects, and neither is about how much work there is — 56 commitments are due in the
next fourteen days.

1. **Capacity is a sum; placement needs contiguity, and nothing reconciles them.**
   `select` spends 215 minutes across a day whose largest free run is 60, so it picks a
   90-minute sitting that `place` then cannot put anywhere. The item overflows *after*
   having already spent its minutes out of `remaining`, so the budget is gone and the work
   is not scheduled. That is how 175 of 183 candidates overflow on a day with three and a
   half free hours.
   Fix: `select` caps a sitting at the largest free slot (`cap.largest_slot`), so what it
   selects is placeable by construction. Not a placement simulation — the cap is the one
   number that makes the two agree, and P1's "never select past capacity" gains "and never
   select past the biggest hole you have".

2. **Rollover outranks priority, permanently.** `order()`'s first key is
   `0 if c.rollover_count else 1`, so anything that has ever rolled beats everything that
   has not, whatever it is due. On 08-26 that handed the day's first 90 minutes to a CIS236
   RFP due **13 November** while seven PSY101 and CIS236 assignments due **28 August** sat
   behind it. P10 ("rollover items appear at the top of the next day's proposal") is a
   statement about *within* a band — a thing you failed to do yesterday leads the things
   you would otherwise do today — not a licence to lead the day with next semester.
   Fix: rollover moves inside the priority band, exactly where `plan/preferences.py`'s lane
   rank already sits, and for the same stated reason.

**Stated assumption:** "more homework time" is read as *the plan should actually contain
the coursework that exists*, not *invent coursework*. The two fixes above are most of it;
the reservation below is what stops one 344-minute milestone eating a day that owes seven
assignments on Friday.

## The day shape the owner asked for

Owner's ruling this session: **homework 120 minutes a day, coding 90**.

- [x] 1. **Homework reservation** (`plan/planner.py`). Coursework — `estimate_source =
      'analyzed'`, the assignments `coursework.py` measured — gets first claim on up to
      `HOMEWORK_TARGET_MINUTES` (120) of the day's capacity. Implemented as a reservation
      inside `select`, not a second pass: non-coursework work may not spend the last
      reserved minutes while coursework is still waiting, and the reservation lapses the
      moment there is no coursework left to want it, so a day with nothing due is not
      held artificially empty.
- [x] 2. **Standing blocks** (`config.py`, `plan/planner.py`). Generalize the `Study`
      block, which is already exactly this shape: a synthetic, capacity-bounded,
      non-rolling block placed after the real work. `STANDING_BLOCKS=coding@90,study@90`
      in the same spelling as `ROUTINES`, each with its own `kind` so `rollover.open_blocks`
      keeps ignoring them — an hour of coding nobody did is not a debt, and rolled it would
      arrive tomorrow as an obligation the owner never made.
      `study` keeps its existing condition (only on a day with no coursework selected);
      `coding` has no condition, because the owner asked for it daily.
      `study_block_minutes` stays as the compatibility spelling for one release.
- [x] 3. **Tests**: a fragmented day (the real 08-26 shape) plans more than one block; a
      90-minute sitting is not selected into a 60-minute day; a November rollover does not
      outrank Friday; the reservation lapses on a day with no coursework; standing blocks
      never roll; both timezones. Each guard mutation-checked, not merely green.

## More logic — four rules, all positive contradiction

The boundary is unchanged and is the reason this is safe to automate: **the ledger holds
the row that disagrees with the row being closed.** Silence never closes anything here;
that is `staleness`, and it asks.

- [x] 4. **A completed reminder closes its commitment.** The fishtank cause, found today:
      Reminders emits `x-apple-reminder://UUID:completed:TIMESTAMP` when an item is ticked,
      triage drops it as noise (correctly — it carries no new obligation), and **nothing
      ever closes the commitment the original reminder created**. Six completed reminders
      ingested, six dropped, **five open commitments** standing behind them right now —
      including three of the five rows that lead the state doc's "this week" section.
      The rule joins on the UUID before the `:completed:` marker and resolves with that
      item cited. `resolve`, not `drop`: the owner did the thing.
- [x] 5. **An upstream due date that moved.** Goal 4 increment B1, designed and unbuilt.
      Five CIS236 assignments moved (1-1-1 through 1-1-4 to 08-25, the Team Charter to
      09-04) and the ledger still holds the old dates, because the re-read produced an
      immutable-content conflict that was logged and dropped. The `assignment` table is
      the mirror that legitimately changes; where its `due_at` differs from the
      commitment's, the commitment moves, with the old value and the feed's read time in
      the note and a `deadline-moved` notification through notify's dedup.
- [x] 6. **A retracted source item takes its commitment with it.** `retraction.py` already
      certifies that an item is gone from a windowed complete read. A commitment whose only
      evidence has been retracted is standing on nothing: tombstoned, citing the retraction
      row. This is the rule that makes retraction mean something downstream.
- [x] 7. **Duplicate obligations ask, never dispose.** The five-UT-Dallas-rows shape: one
      promise extracted several times. Which row survives is a judgement (they differ in
      wording, due date and source), so this one raises **one question per cluster** using
      `duplicates.clusters`' existing star shape, and disposes of nothing on its own.
      Deliberately the only one of the four that cannot write.

## Constraints

- No migration if it can be avoided; every one re-arms the sidecar rebuild and the
  scheduler runs the checkout (2026-08-21 lesson). Rules 4-7 read existing tables. The
  standing blocks are configuration, not schema.
- `logic.py` stays deterministic and model-free. Rule 5 *moves* a row rather than closing
  one, which is a new verb for that file — it goes in with its own disposal kind so the
  Decisions page can tell a move from a close.
- Every rule gets a near-miss test, in that file's idiom: the tests are the boundary.

## What landed (2026-08-24, branch `worktree-planner-logic`)

**The planner half** (commit 1). `Capacity.largest_slot`; `select` clamps divisible work
to it and overflows indivisible work that fits in no hole; rollover moved inside the
priority band; the homework reservation; `Settings.coding_block_minutes` and a shared
`standing()` placer sized against the largest *remaining* run. docs/04 gains P21 and P22
and corrects P10 and the §5 bullet, which the code had been overriding since they were
written. Measured on the owner's live 2026-08-26: **30 → 190 planned minutes**, four work
blocks (three of them PSY101 work due the 28th) and a Coding block at 20:00; 08-27 goes to
280 of 315. Nine tests, three of which were rewritten after mutation testing caught them
green with the feature removed — a reservation changes nothing on a day with capacity to
spare, so they run against a deliberately scarce 135-minute day now.

**The logic half** (commit 2), all four rules, no migration:

- `_completed_reminders` — the fishtank cause. Joins on the reminder UUID before the
  `:completed:` marker, resolves (the owner did the thing), and leaves an untouched
  reminder alone, because that is silence and silence belongs to `staleness`.
- `_evidence_was_retracted` — drops a commitment whose source item `retraction.py`
  certified gone from a windowed complete read. Dropped, not resolved: nothing says the
  work happened. This is what makes the retraction table mean something downstream.
- `_upstream_due_dates_moved` — goal 4 B1. The `assignment` mirror against the
  commitment, compared on `substr(due_at, 1, 10)`. A **move**, the first verb in that file
  that changes a row instead of closing one, so it gets its own action and its own
  `claim_event` and a `deadline-moved` notification. Its idempotency test is the one that
  matters: nothing about its own disposal stops it re-running, only the row now agreeing
  with the feed.
- `questions._duplicate_obligations` — deliberately in `questions.py`, not `logic.py`,
  because it asks and cannot write. `logic.py`'s contract is disposal; a rule that asks
  does not belong in it. Answering "same promise" runs `actions.same_thing`, the board's
  own button.

**A number that changed after measuring rather than reasoning.** The duplicate ask floor
was 0.75 for about an hour. Then "send the housing form to Barrett" and "send the housing
deposit to Barrett" — two genuinely different obligations — measured **0.83**. The floor is
now `dedup_threshold` itself: ask only about pairs the auto-joiner would have merged had
they arrived together. The measured pair is the near-miss test.

15 tests; suite 2467 → 2482. Every guard mutation-checked: breaking the UUID join, turning
the retraction drop into a resolve, dropping the date inequality, removing the ask floor,
removing the batch cap and removing the answer hook each turn a specific test red.

# Handoff — model routing, Slack, triage (2026-08-24)

One session, four workstreams. Two shipped, one is blocked on the owner, one is a plan
awaiting approval. Each has its own document; this section is the index and the state.

| # | workstream | document | state |
|---|---|---|---|
| 1 | Slack as a live source | `tasks/slack-source-2026-08-24.md` | **built, needs a token** |
| 2 | Model provider failover | `tasks/model-routing-2026-08-24.md` | **shipped, live** |
| 3 | Free inference capacity | `tasks/free-inference-2026-08-24.md` | **researched, needs an account** |
| 4 | Better triage | `tasks/triage-plan-2026-08-24.md` | **plan only, nothing implemented** |
| 5 | Lectures as a source (Slidescribe) | `tasks/slidescribe-integration.md` | **parked: Canvas read solved, but no lecture recordings exist yet** |
| 6 | GUI for the recent features | `tasks/native-gui-2026-08-25.md` | **Lane A + B2/B2b built and verified; B3 awaiting an owner ruling** |

Suite 2,502 → 2,526 across the session. `ruff` clean on every file touched; `mypy`'s 8
errors in `extract/client.py` are pre-existing at HEAD in `AnthropicAPIBackend`.

## Blocked on the owner — nothing moves until these happen

**H1. Slack token.** Slack has no personal-token button; the route is a self-owned app.
api.slack.com/apps → Create New App → From scratch → pick the workspace → OAuth &
Permissions → **User** Token Scopes (not bot): `channels:history`, `groups:history`,
`im:history`, `mpim:history`, `channels:read`, `groups:read`, `im:read`, `mpim:read`,
`users:read` → Install to Workspace → copy the `xoxp-` token to `SLACK_TOKEN`. Leave
`SLACK_CHANNELS` empty. Then one sync, then choose conversations on **/chats** — nothing is
read until you do. Full detail: docs/07 §Slack, docs/13 §3.

**H2. Groq account.** `console.groq.com`, GitHub login, no card. This is the answer to the
capacity problem: reported ~14,400 requests/day against OpenRouter free's 50, and its
Services Agreement §4.2 says primary-source that Groq *"is not permitted to use Inputs or
Outputs for training or fine-tuning"* — better than the current primary on privacy as well
as volume. **Probe before pasting the key anywhere:**

    uv run python scripts/openrouter_shootout.py --triage \
        --base-url https://api.groq.com/openai/v1 --api-key gsk_... \
        --model llama-3.3-70b-versatile --model openai/gpt-oss-120b

Then the same without `--triage` for the extraction schema, which is the harder test. Take
model ids from what passes, not from any document. `.env.example` carries the exact block.

Both are consent decisions as much as configuration: pasting either key routes private
correspondence to a new third party.

## Ready to run, no approval needed beyond a yes

**H3. `backglass noise`** — the single best free win available. 45 mail senders have ≥5
items each and have **never once produced a keep**, and are absent from `learned_noise`
(117 entries, all promoted 2026-08-13 and untouched since). They have consumed 881 model
triage calls and still arrive at 5.5/day — **14% of all mail, removed before any model
call, permanently**. Check two entries by hand first: `notifications@instructure.com` is
Canvas, and `sundevilparents@reply.asu.edu` is *already* in `NOISE_SENDERS` yet still shows
triage verdicts, which may mean tier-0 matching is not working.

**H4. Measure the triage model, after 00:00 UTC** when the OpenRouter daily quota resets.
`MODEL_TRIAGE` is currently a placeholder — the proven extraction model, chosen because the
configured one had died and nothing could be measured with the quota spent.

    uv run python scripts/openrouter_shootout.py --triage   # pick a cheaper slot-1 model
    uv run python evals/eval_triage.py                      # precision/recall — read recall first

**Trap, created by this session's own work:** `eval_triage.py` builds its client through
`build()`, which now returns the failover chain. A 429 sends the eval to `claude_cli` and it
reports *haiku's* numbers as though they were the free model's, at $0 charged, with nothing
on screen looking wrong. Run it with `MODEL_FALLBACK_BACKEND=` unset, or print
`fallback_stats(client)` and discard the run if anything crossed over. The two tools
together also spend ~24 of the day's 50 calls — they consume the budget they measure.

## Awaiting approval

**H5. The triage plan, T1–T7** in `tasks/triage-plan-2026-08-24.md`. Ordered T4 (noise) →
T3 (measure) → T1 (re-measure escalation, may close itself) → T2 (instrument, only if
needed) → T6/T7 (give chat triage its thread). Three of the seven are measurements rather
than changes, because the two things that looked most broken — escalation and triage
quality — were both being measured through a failing provider. Fix the instrument, then
read it.

## Known-broken, untouched, and older than this session

- **`canvas:ics` immutable-content conflicts.** Five assignments (`7833000`, `7833001`,
  `7833003`, `7833004`, `7833134`) recompute a different `content_hash` for rows already
  stored, so every sync reports them as errors and the re-read propagates nothing. This is
  the 2026-07-30 determinism lesson recurring. Flagged three times today, fixed zero times;
  it deserves its own session.
- **`notes:obsidian` has stored 0 items** while reporting healthy, with a cursor that
  advances (currently 2026-08-24T19:17Z). Hypothesis worth testing first, not a diagnosis:
  the vault is written by Backglass, and `backglass: generated` frontmatter is what stops
  the ledger eating its own output — if that guard now matches every note, the connector is
  correctly excluding the entire vault.
- **`MONTHLY_SPEND_CAP_CENTS=45000` expires 2026-09-01** — eight days out. Put it back to
  `5000`. It was raised only to clear August's $419.33 of *imputed* `claude_cli` spend.

## State of the tree — read this before committing anything

- **Everything is uncommitted.** HEAD is `24773d3`. The working tree also carries ~69 files
  from *prior* sessions, and four files this session edited (`config.py`, `__main__.py`,
  `.env.example`, `docs/07-connectors.md`) already had those sessions' changes in them — so
  a commit of those files **cannot cleanly split**. Decide deliberately; do not discover it
  afterwards. Full diff snapshotted to the session scratchpad as `*.patch`.
- **`.env` is gitignored**, so today's changes to it appear in no diff: the failover chain
  (`MODEL_FALLBACK_BACKEND=claude_cli`), the repaired `MODEL_TRIAGE`, and the reordered
  `MODEL_FALLBACKS`. They are live on the next scheduled sync.
- **launchd runs this checkout**, so uncommitted code is production within ~30 minutes.
- **The OpenRouter key was echoed unredacted into the session transcript** while reading
  the model block. It is a $0-credit account so exposure is low, but rotating it is the
  owner's call.

## What the numbers say, in one line each

- Free tier is spent: **0 of 14** free tool-capable OpenRouter models produced a valid tool
  call today; the `X-RateLimit-Limit: 50` is **account-wide**, so model rotation cannot help.
- The pipeline is therefore running **entirely on the Claude subscription**, invisibly,
  because the failover chain built today absorbs it. `report.fallback_calls` is the number
  that says so — 53 of ~123 calls in the last full run.
- Triage is 85% of volume: 10,757 items, 9,131 dropped, 1,612 kept.

## Fit and the priority list (2026-08-24, owner)

*"find a way to fit it into the day and there should be a priority list in terms of
conflicts."*

**Fit** — three changes, the first a bug. The protected block was never charged to the
budget: it is a fixed 90-minute run holding whatever the head item needs, so a 45-minute
obligation in it gained the day 45 unaccounted minutes. The live 2026-08-24 plan claimed
248 against a capacity of 205, which is why every "capacity left" sizing started negative
and the Coding block silently did not exist. Placement is best-fit now (a fragmented day
has one hole big enough for the hour-long thing, and first-fit gave it to whatever was
ordered first), and anything that fits nowhere whole is placed in pieces — standing blocks
and divisible coursework only, never a single indivisible thing. Divisible work is no
longer clamped to the largest gap, since splitting replaces that.

Measured: 08-26 goes 190 → **215 of 215**, fully packed, Coding split 60 + 25; 08-24 is
honest at 204 of 205; 08-27 places Coding whole.

Found while testing: `protected_block_minutes = 0` produced a **zero-width** protected slot
and swallowed the first real piece of work into it — neither scheduled nor overflowed.

**The list** — `backglass/plan/priority.py`, seven tiers in the owner's order, shown on
/schedule, overridable by the `preferences/planner.priority` fact. Three tiers were already
true by construction and are written down anyway so the list is complete; the rest reaches
the planner as the lane rank it has accepted since August and been handed an empty set
ever since, because that fact had never been written. The priority mechanism had, until
this change, never once altered an outcome.

Tier 2 (due <48h) sits above tier 3 (gym, meals) and **nothing acts on it**: the plan names
the collision in tier order and the owner moves something. A planner that deleted dinner to
fit an essay is the surface nobody trusts twice.

15 tests; suite 2482 → 2496. Six of them were rewritten after mutation testing showed they
passed with the feature removed.

- [~] Measured and declined (2026-08-25, note below): automatic bumping of a flexible routine for a tier-2 item, within
      the existing `ROUTINE_MAX_SHIFT_MINUTES` bound and with the shift stated in the plan.
      Deliberately left to the owner rather than assumed.

## Dependencies landed (2026-08-24) — the state doc's last section is real

`check-relevance@3`. Every verdict now answers **what it rests on**, keeps included, and
the ids go to `claim_dependency` (0032) as goal 3's invalidation index. A `keep` that rests
on nothing records `none`, because "judged to rest on nothing" and "never judged" are
different facts and `claim_events` requires an empty result to read as the second.

**Re-judgment, keyed on row state rather than on time.** A verdict is re-opened when every
dependency it recorded has been superseded — `facts.remember` supersedes a fact,
`invalidate_fact` breaks its dependents, and the commitment is queued again. The first
version compared `claim_event.at` against `logic_check.decided_at` and could not work:
`now_iso()` is second-resolution, so a fact superseded in the same second as the verdict
compares equal, and the fix in either direction is a loop or a miss. Row state has no
ambiguity and clears itself — the re-judgment writes fresh `active` rows, so the condition
is false again the moment the obligation is judged.

`_record` became an upsert in the same change: with re-judgment possible, `DO NOTHING`
would leave a stale verdict looking settled. The history goes to `claim_event`
(`relevance_rejudged`, old verdict → new) rather than to a superseding row, because giving
`logic_check` history needs a migration and that re-arms the sidecar on a checkout the
scheduler runs.

A `pending` verdict is never re-opened: it is already a question in front of the owner, and
asking the model again spends money on a decision waiting on a person.

No migration. 7 tests (suite 2496 → 2503), every guard mutation-checked — dropping the
`none` row, skipping the id intersection, removing the re-queue and reverting the upsert
each turn a specific test red.

Still true and worth stating: `_rests_on` renders empty until the pass actually runs on the
live ledger, which costs model calls. `backglass relevance --dry-run` first.

## The repair loop became callable (2026-08-24)

*"continue to integrate and make the app smarter and more autonomous."*

The chain that repairs the ledger — catchup, replan, logic, questions, notify, situation,
vault — existed as ninety lines inside `__main__.py`'s sync command, with seven bare
`except Exception: pass` blocks. Three consequences, and the fix is one module:

1. **It ran from one entry point.** The 30-minute launchd sync got the whole chain; an app
   opened at 09:00 got `catchup` alone; nothing else repaired anything.
2. **Its failures were silent.** `logic.Report.errors` in particular was collected by the
   checker and dropped by its caller — one rule could raise on every pass forever and every
   surface would still look healthy.
3. **It could not be tested.** No name to call, no report to assert on.

`backglass/repair.py` is the chain as data: an ordered `STEPS` tuple, each step best-effort
but *recorded*, returning lines + errors + which steps completed. The sync command folds
its errors into the run's own, so they reach `run.errors_json` and the Sources panel, which
is where rule 5 says a failure belongs.

**Opening the dashboard now repairs.** Two cheap indexed triggers: a hole (the original
one), or the loop not having run in `OVERDUE_MINUTES` (45) — which encodes the reason this
trigger exists, since launchd cannot be trusted to have fired. Everything after `catchup`
is deterministic and free, so an app that is open is an app that repairs rather than one
waiting up to half an hour for the scheduler.

**Named `repair`, not `reconcile`** — `reconcile` already means "is this source still
collecting" here (`tests/test_reconcile.py`, `_check_reconciles`), and the collision was
caught only because the file-exists guard refused to overwrite that test file. Two unrelated
things under one word is how a reader ends up in the wrong file.

No migration, no `run` row of its own (four readers take the newest row with no `kind`
filter). 11 tests; suite 2551 → 2562 in the checkout. Four guards mutation-checked:
swallowing step errors, dropping the checker's own errors, never reporting overdue, and
ignoring `kind` when asking how long it has been.

Verified live rather than by fixture: `repair.run` over the real ledger ran all seven
steps clean, and a full `backglass sync` produced the epilogue end to end — catchup indexed
two documents, the plan was replaced, a question was raised, the state doc reached v13 and
the vault wrote 11 files.

Noted, not fixed: that sync also logged seven `content changed for an immutable
source_item` errors for Canvas assignments. That is the known conflict class — the
`assignment` mirror plus `logic._upstream_due_dates_moved` handle the *consequence*, but
the error itself recurs on every re-read and is still noise in the Sources panel.

## Source semantics, declared once (2026-08-24)

The last piece of the repair thread, and the noisiest: `content changed for an immutable
source_item` fired seven times in **every** sync, forever, because nothing ever resolves
the condition that raises it. An error that is always present is an error nobody reads,
and it sat in the same panel as the failures that matter.

`connectors/base.SNAPSHOT_SOURCES` is the declaration the codebase never had. A **stream**
source (mail, iMessage) writes a record once, so a changed hash means the extractor changed
or something upstream is lying — still an error. A **snapshot** source (Canvas, calendars,
reminders, notes) hands back the current state of a live record, and a changed hash means
Tuesday.

`source_item` stays immutable either way; only the *report* changes. Seven errors became:

```
7 upstream record(s) changed on canvas:ics — the mirror carries the new values
```

The downgrade is honest because the value is not lost: each snapshot source keeps a mirror
beside the immutable item (`assignment` for Canvas), and `logic._upstream_due_dates_moved`
moves the commitment from there. The error never carried the information — it made noise
where the mirror was already doing the work.

Membership is evidence: every entry has been observed changing, and each absence is a claim
(`files` stays a stream, because a re-saved contract in the drop folder is a real change and
nothing mirrors it yet). A second feed inherits its family — `calendar:work` is a calendar —
so a new source cannot silently fall back to stream, which is the fallback that produced the
noise.

7 tests, both guards mutation-checked; docs/07 gains the section. Live: the dry run's seven
errors are one line.

- [~] Closed 2026-08-25 (note below): `files` has no mirror, so a changed document is still only an error.
- [~] Half already built, half no live instance (note below): calendars — a moved class is reported and nothing
      downstream acts on it the way the assignment rule acts on a moved deadline.

## The situation block was showing archaeology (2026-08-24)

pipeline-audit §4a, deferred twice and now closed. `context._situation` is the block
**every** model call carries — triage, extraction, the relevance judge — and it ordered its
named rows `due_at ASC`. That sounds right and is exactly backwards: the overdue set only
ever grows at its old end, so the five lines labelled "the owner's current situation" were
the five most-lapsed rows on the board, permanently.

Measured on the live ledger before the fix, with 37 things due inside the week:

```
- 420 open commitments: 53 overdue, 37 due within 7 days
- the owner owes: "MCAT prep" (due 2026-03-23 OVERDUE)
- ... "complete Sun Devil Ready orientation module" (due 2026-04-11 OVERDUE)
- ... "Email list of transfer credits before appointment" (due 2026-04-12 OVERDUE)
- ... "email list of transfer/AP credits" (due 2026-04-12 OVERDUE)
- ... "send transcript and AP scores" (due 2026-04-13 OVERDUE)
```

Five obligations from March and April, and not one of the 37. After — the same query, the
same day, ordered by the nearest edge of *now* in both directions (soonest first for what
is coming, newest lapse first for what is gone):

```
- ... "Complete CIS236 assignment 1-1-1 Tech in the 21st Century" (due 2026-08-25)
- ... "Complete module 1-1-2 What is an Information System"       (due 2026-08-25)
- ... "Complete '1-1-3 - What is Analytics?' course module"       (due 2026-08-25)
- ... "Complete 1-1-4 What is Artificial Intelligence video"      (due 2026-08-25)
- ... "Complete Module 1 Scientific Reasoning Homework 1"         (due 2026-08-25)
```

Those are the same four CIS 236 rows whose deadlines the upstream-due rule corrected this
morning, so two fixes now compound. Nothing is hidden by the reordering: the counts line
above is still over the whole board, 53 overdue is still 53 overdue. The state doc's week
section reads from the same function and improved with it (v16).

4 tests, both directions mutation-checked. No migration.

Seen while reading the output, not fixed: the plan lines show "Barrett advising appointment
with Rachel Espericueta" and "Advising with Rachel Espericueta" on the same day. Duplicate
detection covers commitments and never covered engagements — the belief-lifecycle
divergence (pipeline-redesign §4, item 2) in one visible line.

## Two plans, one day — the affordance the trade-off assumed (2026-08-24)

Owner said to decide, so this was decided by measurement rather than taste. Three
candidates were on the table; two were dropped for the same reason.

- **A calendar mirror** (a moved class nothing acts on): zero live instances today.
- **The timestamp-shape bug** — `_agrees` compares `starts_at[:16]` as *strings*, so
  `2026-08-28T09:30:00-07:00` and `2026-08-28T16:30:00+00:00`, the same instant, read as
  different plans. Real, and **zero** live pairs are caused by it. Left recorded, unbuilt.
- **Duplicate plans**: 127 same-day near-duplicates on the live board. Attributed rather
  than assumed — 120 fail on *wording* (below the 0.85 the matcher requires), 4 on the
  documented clock policy, 0 on the shape bug. So the matcher is working exactly as
  designed, and what is missing is the thing its own justification leans on:
  *"a duplicate is visible on the board and can be dismissed."* For engagements it was
  visible nowhere and dismissable never.

Built: migration 0034 (`engagement_distinct`, `commitment_distinct`'s shape one table
over), `scrub.duplicate_plans` (same day required, wording floor 0.6 — below
`dedup_threshold` on purpose, since at or above it the matcher would already have merged
them), `actions.same_plan` / `actions.different_plans`, and a pairs section on /scrub with
Same plan / Both real.

The merge keeps the older row and folds the newer with its evidence and its guest list —
and **never repaints the winner's hour**. A commitment merge takes the earlier due date
because a deadline is a fact; a plan's hour is a decision somebody made, and the two rows
exist precisely because nothing said which hour replaced the other. Picking one would be
the silent repaint `_same_row` refuses to make.

12 tests, three guards mutation-checked. Live: 125 pairs on the board, top of the list
"Pih ball meetup" 19:30 against 19:40.

- [~] Verified harmless where it would matter (2026-08-25, note below): the `starts_at[:16]` string comparison, and the three
      timestamp shapes (138 date-only, 126 naive, 20 offset-bearing) living in one column.

## An assignment that left the feed (2026-08-25) — goal 4 B2 closed

No new mechanism was needed and none was written. `retraction.reconcile` has always
retracted a connector's rows that its latest *certified complete* read did not return, and
`sync` has always called it for every connector. Canvas simply never certified.

`CanvasIcsConnector` implements `Reconcilable` now. Three guards, and each is one of the
near-misses `retraction.py` documents from the day it came one clean run from retracting
sixteen live classes:

- **`read_complete`** — a read that raised, or one a caller abandoned partway, certifies
  nothing. "The feed returned nothing" and "the feed no longer has anything" are the same
  bytes to everything downstream. The two obvious tests for this were *vacuous* (both fell
  through the empty-set check); the case it actually guards is a partial read, and that
  test is what makes the guard bite.
- **`seen_ids` records what the store returned, not what was emitted** — captured before
  the boundary check, because an assignment the boundary excludes is still one Canvas
  published, and recording only the emitted set is the exact near-miss.
- **The window is the span of what came back**, not all of time. An assignment deleted at
  the very edge shrinks the span past itself and is missed this run — a missed retraction
  rather than deleted history.

Live: the feed certified 199 of 199, and the would-retract list was **one row** —
`assignment:7833072`, "EC - Flowcharting **(Old)**". The 196 in-window rows were untouched
and the 2026-08-23/24 rows sat outside the window entirely, which is the conservative bound
working.

Run for real, the whole chain fired in one sync with nothing else prompting it:
`retracted 1 item(s)` → `logic check disposed of 1 item(s): evidence-retracted ×1` →
commitment 492 dropped citing the retraction and its window → situation v19 → vault.

Also fixed: the sync line said "your calendar changed", written when calendars were the
only source that could certify. The first live Canvas retraction reported a deleted
assignment as a calendar change.

9 tests, three guards mutation-checked.

## D1 measured and declined (2026-08-25)

Goal 4 D1 wanted a model to read an assignment's description and estimate effort where the
text states no number. It was designed when 120 of 143 open assignments carried a flat
`type_default:30` and the deterministic reader did not exist yet. Both halves of that have
changed, so it was measured before being built.

Of 199 assignments, 71 now carry a **stated** basis — a runtime, a chapter range, a word or
page count read off the text by `coursework.py`. The remaining 128 come from the type
table, and that is the population D1 targets. Reading them:

- **52 have no description at all.** A model has nothing to read.
- 76 have one, median 174 characters — mostly boilerplate.
- **5** of those 76 state any number a reader could use, and most are false positives: the
  "48 hour" in a homework description is the late window, and the "10 minute" in "Schedule
  Here! Lab 2" is an appointment slot.

So the information D1 assumed is in the descriptions is not in the descriptions. A model
asked how long "Complete the LearningCurve activity" takes would be inventing a number and
presenting it as a measurement, which is worse than the type table — the table is at least
the owner's own calibration, stated once in configuration where they can change it.

Declined, not deferred. Reopen it only if the feed starts carrying rubrics or point values,
which would be a new fact about the source rather than a new idea about the model.

## The mixed timestamp shapes, chased to their consequences (2026-08-25)

`engagement.starts_at` holds three shapes side by side — 138 date-only, 126 naive local,
20 offset-bearing. That looked like a latent correctness bug worth fixing. It was chased
to every reader that could turn it into a wrong answer, and it does not.

- **The schedule and capacity are already right, explicitly.** `capacity.engagement_events`
  selects on `substr(starts_at, 1, 10)` and its comment says why in full: handing the
  mixture to SQLite's `datetime()` normalises the offset-bearing rows to UTC and marches a
  19:00 Phoenix dinner into the next day. And `_aware()` converts an offset-bearing value
  into the owner's zone for rendering, because a UTC row once printed a 10:30 lecture as
  "17:30" beside a `-07:00` row that printed correctly. Both hazards were found and fixed
  before I looked.
- **Dedup is the one place it still bites**, at `_agrees`'s `starts_at[:16] == ...`, which
  compares wall-clock digits so the same instant in two shapes reads as two plans.
  Measured across the whole live board: **zero** duplicate pairs are caused by it.

So: a real inconsistency, no observable consequence, and the two readers that matter
already handle it deliberately. Left alone rather than "cleaned up" — normalising the
column would be a data migration over 284 rows to fix nothing anyone can see, and the
comments recording *why* each reader is careful are worth more than a uniform column.

Recorded so the next reader does not re-derive it.

## The routine auto-bump, measured and declined (2026-08-25)

The owner's priority list puts tier 2 (due within 48h) above tier 3 (gym, meals, sleep), so
"let a paper due tomorrow take the gym hour, bounded and stated" was the obvious next
increment. It was offered, not taken, and then measured before being built.

Proposing every day of the coming week:

```
2026-08-25  capacity 425  planned 417  left  8   1 item due <48h did not fit
2026-08-26  capacity 215  planned 195  left 20  13 items
2026-08-27  capacity 315  planned 300  left 15  12 items
2026-08-28  capacity 450  planned 446  left  4  17 items
2026-08-29  capacity 570  planned 550  left 20  23 items
2026-08-30  capacity 570  planned 550  left 20  24 items
2026-08-31  capacity 285  planned 275  left 10  34 items
```

The conflict fires **7 days out of 7**, which at first reading argues hard for building it.
It argues against. Every one of those days is already planned to 95-99% of its capacity —
the day is not being wasted on the gym, it is *full*. Bumping a routine buys 60 to 180
minutes against a backlog of 13 to 34 obligations, so it would cost the owner their gym and
their dinner and still not fit the work.

The mechanism was never the problem. **The week is over-subscribed**, and no scheduling
policy fixes that; the honest response is the one the plan already gives — name what did
not fit and let a person decide what to drop. A planner that quietly ate the routines to
make the arithmetic close would be hiding exactly the fact the owner most needs.

Declined. Worth reopening only if a day ever shows a tier-2 item failing to fit while
meaningful capacity sits unspent — which is the shape this was imagined for and is not the
shape the ledger has.

**Left for the owner, because it is not a scheduling question:** items due within 48h are
climbing (13 → 34 across the week) against days of 215-570 minutes. That is a real overload
and it is theirs to resolve, by dropping work or moving deadlines.

## The last two mirror items, closed (2026-08-25)

**Calendars are already half done, and I had not noticed.** `apple_calendar` has
implemented `retractable_window` all along, so a calendar event that *disappears* is
retracted exactly like the Canvas assignment closed above — that is the incident the whole
retraction module was written for. What is genuinely unhandled is an event that **moves in
place**, keeping its UID: the source item is immutable, the change is now correctly
reported as an upstream change rather than an error, and the old time stays in the ledger.

Live instances of that today: **zero**. The last read showed 7 upstream changes, all
`canvas:ics`, none on either calendar. So the fix would be built blind, and the assignment
work is the template for whoever builds it with a real case in hand.

**`files` stays an error, deliberately.** A re-saved document in the drop folder is a real
change to evidence the owner may be relying on — a contract, a letter, a syllabus — and
until something mirrors it the error is the only notice they get. Downgrading it to a
counted "upstream change" would make the system quieter about the one source where quiet is
wrong. This is already written into `SNAPSHOT_SOURCES`'s reasoning; it is closed here so it
stops reading as an outstanding task.

With these two, the plan file has no open items whose value survives measurement. Four were
built this session, four were measured and declined with the numbers recorded, and two were
found already done.

