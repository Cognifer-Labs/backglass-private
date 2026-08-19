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

## Constraints that bite

- Any migration re-arms the frozen-sidecar crash (`matches_source` already false);
  every increment that adds one notes the rebuild step in its commit.
- Prompt changes are version bumps in `specs/extraction-prompts/`; re-extraction is a
  deliberate owner decision (drop the `compatible:` line), never a side effect.
- Cost tracks output, not input (2026-08-07 audit) — richer context per call is
  affordable; do not pre-optimize it away.
- No test calls a live API; every new prompt gets fixtures.
