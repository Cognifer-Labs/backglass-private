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

## Goal 3 (2026-08-23): the loop is one thing, and it says what it did

Owner's directive: better loop commands, better triage and processing, more autonomy,
less dependence on the owner sitting in a terminal, and more efficient.

**Stated assumption, so it is not invisible:** "loop commands" is read as *the recurring
autonomous pass set*, not an in-process scheduler. launchd owns cadence
(CLAUDE.md decisions table) and every command added here is one-shot. A `while True:
sleep(1800)` would re-open a closed decision.

### What the loop is today, read rather than remembered

`launchd → backglass sync` runs `sync()` (ingest → rules → triage → extract → recheck →
relevance → reviews) and then **five more passes wired inline in `__main__.py:429–493`**
as ad-hoc `try/except: pass` blocks: catchup → replan → logic → questions.refresh →
notify. Four defects follow from that shape, none of them loud:

1. **Two entry points, two different pass sets.** The CLI runs all five. App-open
   (`catchup.spawn_on_open`) runs *catchup only* — no replan, no logic check, no question
   detection, no notification. The owner opening the app gets a third of the loop.
2. **`except Exception: pass` is not rule 5.** The rule is log, surface, continue, exit
   non-zero. Today a pass that has been throwing for a week is indistinguishable from a
   pass with nothing to do: no row, no panel, no exit code, no `state` field. A loop you
   cannot see failing is unattended, not autonomous.
3. **The lock covers one pass of five.** `catchup.run` re-takes `run_lock`; `replan` —
   which *supersedes day plans* — runs outside it, so an app-open catchup proposing a
   plan and a sync replanning it can interleave.
4. **Nothing is gated.** 48 syncs a day each run 5 full detector sweeps whether or not a
   single row was written. Verified model-free (`logic.py` and `questions.py` hold zero
   `ModelClient` references), so this is CPU and wall time rather than money — but it is
   the reason a sync that ingested nothing still takes seconds.

And `duplicates` — the star-clustering work of increment 9 — is reachable **only** by
typing `backglass duplicates` in a terminal. `grep` over the package: one caller,
`__main__.py:2163`. `plan_clusters` (duplicate engagements) has no surface at all. That is
the literal shape of the owner's complaint.

### Increments (each lands tested + committed before the next starts)

- [x] 1. **`backglass/loop.py` — one registry, one runner, zero behaviour change.**
      Each pass declares its name, its trigger (`clock` | `data` | `always`), whether it
      can spend, and its callable. Order preserved exactly: catchup → replan → logic →
      questions → notify. The runner owns the lock contract (take once for the whole set;
      skip, never block, on `SyncLocked`) so callers cannot get it wrong — fixing defect 3
      as a side effect of having one place to put it. `sync_command`'s hundred inline
      lines become a loop over `loop.run(...)`. Idempotency test: run twice on a frozen
      fixture, zero writes on the second.
      landed (bc793f0): `backglass/loop.py` with `PASSES`, `Outcome`, and a runner that
      takes the lock once for the whole set and resolves `now` once for all five;
      `sync_command`'s hundred lines became four. 15 tests, order pinned because it is a
      decision. A side fix went in first (1b087a9): `notify.record` stamped `created_at`
      from the wall clock while `local_date` came from the injected `now`, so the
      re-banner test passed until 2026-08-20 and failed on every machine after it.
- [x] 2. **Per-pass outcomes recorded and surfaced** — rule 5's missing half. Migration
      0030 `loop_pass` (nullable `run_id`, name, started/finished, status ok|failed|skipped,
      detail). `state` verdict "every loop pass ran within its cadence"; heartbeat alert
      for a pass failing every attempt past a grace window; the CLI exits non-zero when a
      pass failed, leaving sync's own code alone when they are fine. **Re-arms the frozen
      sidecar crash — rebuild step goes in the commit.**
      landed (4e22113): migration 0030 `loop_pass` (nullable `run_id`, trigger, status,
      span, detail); `SyncReport.run_id` carries the sync's id out to the rows;
      `loop.health()` enumerates the *registry* so a pass nobody called is reported by
      name rather than absent; a skip is neither success nor failure, so a contended lock
      does not alarm. `state` verdict "every loop pass is succeeding", stderr line and
      exit 1 from `sync`. 23 tests.
- [x] 3. **`backglass loop`, one-shot** — runs the owed pass set without the ingest and
      model pipeline. `--dry-run` prints what is owed and why it is owed; `--only <name>`
      for one pass. This is the "better loop commands" ask, and the point of increments
      4–6 is that the owner should never need to type it.
      landed (df3ae55): bare run / `--only <name>` (repeatable, unknown name exits 2
      rather than silently running nothing) / `--dry-run` as a *read* of where each pass
      stands, deliberately not a rehearsal — these passes deliver notifications and
      supersede plans. A quiet loop says "nothing owed"; a contended lock is said out
      loud, unlike inside `sync`, because someone typed this and is waiting. 8 tests.
- [x] 4. **App-open runs the whole loop**, not a third of it. Safe by construction rather
      than by care: every pass is idempotent and owed-gated, which is exactly what
      increment 1 made checkable. Still a daemon thread, still fire-and-forget, still
      degrades per rule 5.
- [x] 5. **Measured, and the gate was the wrong fix.** The plan here was to skip `data`
      passes whose inputs had not changed. Measured first, on a `.backup` copy of the live
      59 MB ledger, because a gate that wrongly skips is a silently missed detection —
      the failure class four lessons cover — and it is only worth that risk if the work
      it skips is expensive.

      It was expensive, and not for the reason a gate would have fixed. `logic.run` cost
      596 ms and `questions.refresh` 613 ms, and the profile put ~90% of both inside
      `capacity.fixed_events`, called 42 and 46 times respectively — once per day of the
      horizon. Its predicate wraps the column: `datetime(occurred_at) >= datetime(?)`,
      which makes any plain index unusable, so each call scanned all 10,533 `source_item`
      rows to find at most 317 calendar ones.

      Migration 0031 is a partial expression index matching that predicate exactly.
      Measured on the same copy:

          logic.run          596 ms  →   8 ms   (70×)
          questions.refresh  613 ms  →  65 ms   (9×)
          planner.propose    282 ms  →  26 ms   (11×)

      The loop's deterministic half went from ~1.2 s per sync to ~73 ms — better than
      skipping it, because nothing is skipped. **Gating is therefore rejected, not
      deferred**: there is no longer enough work to be worth the risk of not doing it.
      The `trigger` field stays; it is read by `--dry-run` and written to every row, and
      it documents what drives each pass.

      `capacity.CALENDAR_DAY_SQL` is now a constant so the test asserts the plan for the
      query the code issues. The first version of that test passed on a broken predicate:
      a partial index can be chosen for its WHERE clause alone, so "the index is used" is
      not the check — "the plan binds the expression" is. Both mutations run red.

      Still un-gated and now the loop's largest cost: `duplicates.clusters` at **3.7 s**,
      untouched by the index because it is O(n²) — 51,443 difflib ratios and 23,871 cosine
      comparisons over 386 open commitments. That is increment 6's problem, and it is why
      increment 6 bounds and gates that one pass where its inputs live.
- [x] 6. **Duplicates leaves the terminal.** The clustered review becomes a loop pass that
      raises cards on the page where `same`/`distinct` already live, bounded per refresh
      like staleness's five. Auto-collapse stays owner-gated — a row that vanishes with
      nothing saying why is the failure four lessons already cover.
      landed: `duplicates.questions_for` (five cards per refresh, `clusters()`'s own
      order — identical text first), a `duplicates` loop pass gated to once per owner-local
      day (the clustering is 3.7 s, O(n²), untouched by any index), `questions.record`
      split out of `refresh` so an expensive detector reaches the ask-once key through the
      same door, and `_apply_duplicate_answer` acting through the board's own
      `same_thing`/`different`. `Cluster.merged_into` names the row the merge keeps, so
      the card says what the click does. 14 tests, including the card read and answered
      through `/ask`.
      Two corrections found while building it, both worth stating. The once-a-day gate
      first read `finished_at`, which is wall-clock UTC and stamped whatever day is being
      processed — the same two-clocks-in-one-row defect fixed in `notify` at the start of
      this branch, reintroduced three increments later. `loop_pass` now carries
      `local_date` like `notification` does. And "keep them apart" records only the pairs
      the card was built from; recording every combination would assert a judgement about
      two rows that were never compared.
      **Not done: duplicate engagements (`plan_clusters`).** 41 days of them on the live
      ledger, and unlike commitments there is no established resolution action for a
      duplicate engagement — `reject_plan` declines it, which is a different claim from
      "this is the same plan twice". The CLI already says this is resolver-phase policy
      rather than report policy, and inventing it inside a review card would be deciding
      it by accident. Left as its own increment.
- [x] 7. **Owner question, not a flip** — and the premise was half wrong, found by
      looking. The plan said domain candidates were the CLI-only ones. On the live ledger
      there are **zero** domain candidates and **21 address** candidates, and those were
      equally unreachable: `noise.candidates` had exactly one caller, the CLI.
      landed: `noise.questions_for` (five per refresh, strongest evidence first, the card
      naming which *kind* of evidence it is — a model drop and a keep the expensive pass
      then proved empty are different observations), a daily-gated `noise` loop pass
      (`candidates` mines the whole triage history: ~1.2 s), and `promote_value`, which
      re-derives the candidate at answer time so a sender that earned its place between
      the asking and the answering is not silenced on stale evidence.
      `noise_auto_promote` stays off, and a test asserts it. The card is why: the flag
      saves one click and makes "why did I stop seeing mail from my landlord"
      unanswerable, while a card leaves a decision row naming who decided. "Keep reading
      it" deliberately writes no row — ask-once is what makes it durable, and a
      `learned_noise` row meaning "not noise" would be a second store for the same fact
      that can disagree with the first. 6 tests.
- [x] 8. **Triage efficiency: measured, and it is the wrong target.** `model_call` over
      the last 14 days on the live ledger:

          tier          calls    imputed $   total_s
          extract        2909      336.35     69620
          triage          396       19.59     14486
          triage_batch     54        9.77      5038
          recheck          43        3.31      1142
          relevance        16        2.65       516

      Triage is **5% of the spend and 17% of the model latency**. Reworking it would be
      optimising the wrong thing, and the batch path and the parallel pass it would
      target already exist. No rewrite; the measurement is the deliverable.

      **What the measurement found instead, and it matters more than the efficiency
      question.** Last 7 days, per model:

          nvidia/nemotron-nano-9b-v2:free          310 calls,  67 ok   (22%)
          nvidia/nemotron-3-super-120b-a12b:free   107 calls,  51 ok   (48%)
          sonnet                                   249 calls, 244 ok   (98%)
          haiku                                    120 calls, 120 ok  (100%)

      The two free OpenRouter models carry the triage and extract tiers, and since the
      2026-08-21 switch the triage tier has failed **78% of its calls** — 142 errors on
      08-21, 87 on 08-22. Rule 5 is working exactly as designed, which is why nothing
      complained: failed items stay pending, the next sync retries them, and the backlog
      is genuinely zero (untriaged 0, kept-not-extracted 0). The cost is landing on
      latency and retries rather than on correctness.

      Not fixed here, because it is a model-choice decision and the owner's to make: the
      remedy is to re-measure with the shootout script and move the triage tier off
      `nemotron-nano-9b-v2:free`. Stated rather than quietly absorbed — a tier at 22%
      success is one bad day from being a real backlog, and "autonomous" and "retrying
      four times to get one answer" are not the same thing.

### RESOLVED 2026-08-23: the migration-number collision

Found by pointing a measurement script at a `.backup` copy of the live ledger, which
refused to migrate:

    MigrationError: migration 0030 was applied as '0030_source_item_retraction.sql'
                    but is now '0030_loop_pass.sql'

Three migrations were staged-but-uncommitted in the main checkout and already applied to
the live ledger, because launchd runs backglass out of that working tree. `backglass-43`
committed them and added a fourth, and this branch's two were the second claim.

Closed by merging `fix/schedule-canvas-overflow` into this branch and renumbering:
`0030_loop_pass` → `0034_loop_pass`, `0031_calendar_instant_index` →
`0035_calendar_instant_index`, both resealed in `FROZEN_CHECKSUMS`, `specs/schema.sql`
regenerated. A `git mv` leaves the bytes alone, so the two checksums are unchanged under
new keys, and renaming was legal only because neither had ever been applied to a real
database. Migrations run 0001–0035 with no gap, which is what
`test_init_creates_the_schema` asks and why the rename could not happen before the merge.

Verified the way the collision was found, which is the only verification worth having
here: a fresh `.backup` copy of the live 59 MB ledger now migrates clean, applying
`[33, 34, 35]` and reporting 35 as highest. 2532 tests green with nothing deselected.

**Still true and still owed before this reaches the live ledger:** two migrations means
the frozen sidecar is re-armed. Rebuild the desktop app or it will not start against this
schema.

### Settled 2026-08-23: the pass order (audit §1c), and the batch hole (§1a)

`tasks/pipeline-audit-2026-08-21.md` — which this branch was built without knowing existed
— was raised by `backglass-43`, and it was right on both counts. Both landed here at that
session's request; the audit's items are its branch's, these two are this one's.

**§1c, the order.** Now `logic → questions → catchup → replan → duplicates → noise →
notify`. It ran `catchup → replan → logic → questions` for as long as the chain existed,
which put replan *ahead* of the disposal: a logic disposal changes the open set, which
changes the planner pool, which changes `inputs_fingerprint`, so replan compared today's
plan against a world logic was about to edit and the drift it should have caught arrived
thirty minutes later or at 05:45. Nothing was ever wrong in the ledger; the board was
half an hour stale every time the checker did anything. Increment 1's zero-behaviour-change
rule preserved that order and a test then pinned it — the test was right about a smaller
claim (logic before questions) and silent about the larger one. Two tests now pin the
larger one, and the registry made the change a tuple reorder.

The card passes sit after the planning passes rather than before: a card changes nothing
the planner reads, because the merge happens on the owner's answer. They stay ahead of
notify, whose questions-waiting decider counts what they raise.

**§1a, the batch hole.** `batch.collect` applied hundreds of extractions and triggered no
disposal, no detection, no replan and no notification — overnight batch mode is when the
largest change to the ledger happens, so it was the entry point that needed the loop most
and had it least. It now runs inside the collect's own `run_lock` (reentrant within a
process), so the collect and the recognition over its results are one critical section and
no sync can slot between them to plan around a half-recognised board. Only when something
was applied: a collect that found no outstanding batches changed nothing, and the
thirty-minute sync owns the clock-driven passes.

Reported through `CollectReport` rather than echoed — `loop_lines`, `loop_failed`,
`run_id` — so every caller of `collect()` sees it, and a failed pass reaches the exit
code. The audit's complaint about the CLI owning the chain applied to its output too.

### Corrected 2026-08-23: the model-failure number was a flap, not a state

Increment 8 reported the triage tier failing 78% of its calls and framed it as current.
`backglass-43` re-derived it independently and the average hid its own shape. Confirmed
here against the same copy, per day:

    triage tier   08-17  0%   08-18  0%   08-19  0%   08-20  0%
                  08-21 78%   08-22 78%   08-23  9%
    extract tier  08-20 22%   08-21 13%   08-22 36%   08-23  0%

Two bad days, on either side of which the free models answer normally. So "the loop burns
4× the calls it needs" was true on 08-21 and 08-22 and is not true now, and a model swap
decided on the seven-day average would be tuning against a flap. The finding stands only
as: this backend flaps, and nothing surfaces it on the day.

That surfacing is `backglass-43`'s work, not this branch's — `modelhealth.py` ranking the
routed array by measured success, plus a `state` verdict. Noted so it is not duplicated.

### Verified against the live ledger after the merge, 2026-08-23 — two corrections

The index measurement in increment 5 was taken before the merge folded a
`source_item_retraction` join into `CALENDAR_DAY_SQL`. Re-run as a proper A/B on the
merged code and one copy of the live ledger — same database, same process, index dropped
for the counterfactual:

                        with index    without    ratio
    logic.run                 11 ms     612 ms      55×
    questions.refresh        137 ms     743 ms     5.4×
    planner.propose           41 ms      71 ms     1.7×

The join costs nothing measurable and the plan still binds the expression. **Correction:**
increment 5 reported the planner at 282 ms → 26 ms (11×). The honest current figure is
1.7×. The earlier pair was measured across two different database states rather than as
an A/B, which flattered it; `questions.refresh` also does more work now than it did then.
The headline claim — the loop's deterministic half costs milliseconds instead of over a
second — holds, and for `logic` it is better than reported.

**Rule 3 over the whole loop needs three runs on live data, not two.** The fixture test
settles on the second run and passes. On a copy of the live ledger, run 2 wrote one more
`decision` row — a machine disposal of a question run 1's own passes had left behind — and
runs 3 through 6 wrote nothing. It converges; it does not oscillate, which is the
distinction that matters, because an oscillation would write a row every thirty minutes
forever (the 13-briefs-a-day shape in lessons.md).

I did not isolate which row, and am not claiming to have: the full loop reproduces it and
a logic+questions-only loop on a fresh copy does not, so it depends on what `replan`,
`duplicates` and `noise` do in the same pass. Stated rather than tidied away, and pinned
by a convergence test that the two-run assertion could not have caught.

Not a regression from this branch: `logic` ran before `questions` in the old chain too.

### Rendered against the owner's real ledger, 2026-08-24 — one card defect, one open question

Increment 6 and 7 were tested through `TestClient` against fixtures. Fixtures are not the
app, so both card kinds were rendered from a copy of the live ledger. Both work, and the
content is the content they were built for — the first duplicate card is #218/#226, one
ASU ID photo upload written down twice under two entity names, at a 1.00 match.

**Defect found and fixed.** The noise card read:

    Most recent subject: Dharsan- Your Approaching Scholarship Deadlines
    Why triage dropped it: no letters or digits

Both lines true and together nonsense to a reader: the rule reads the *extracted body*,
and an HTML-only marketing mail extracts to nothing. On a question whose answer
permanently stops mail arriving, a card that appears to reason from something false is
worse than no card. Now "dropped one of them", with a line naming what the rule reads.
The `Seen` line lost its ISO seconds and offsets in the same pass — a card a person reads
should not spend half a line on `T14:57:01+00:00`. Two tests.

**Open, and not this branch's to settle: the queue is 41 deep and the new cards are last.**
`/ask` shows one question at a time ordered by `asked_at`, so the ten cards these two
passes raise sit behind 31 older ones — 16 `priority`, 5 `stale`, 5 `nonsense`, and the
rest. The passes announce "5 duplicate cluster(s) to settle — /ask" and the owner arrives
at a backlog with the new thing at the bottom.

Not a regression and not caused here: FIFO is the existing order for every kind, and
changing it is a product decision about all of them, not about these two. Named because
"the cards exist" and "the owner will see them" are different claims, and only the first
one is proven.

### Constraints that bite (carried forward)

### Constraints that bite (carried forward)

- Migration 0030 re-arms the frozen-sidecar crash; the rebuild is part of increment 2.
- No new scheduler. launchd owns cadence.
- No pass may become load-bearing on retrieval (CLAUDE.md's additive ruling).

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

- [x] A1. **Migration 0031** — `commitment_dependency`:
      `(id, user_id, commitment_id, dep_key, kind, fact_id, depends_on_commitment_id,
      quote, reason, status, superseded_by, created_at)`, `UNIQUE (user_id,
      commitment_id, dep_key)`. `dep_key` is a text discriminator (`fact:5`,
      `commitment:42`, `none`) rather than a multi-column UNIQUE, because SQLite treats
      NULLs as distinct and a NULL-bearing UNIQUE enforces nothing — that constraint is
      rule 3 for this table, so it must actually hold. Same migration adds
      `situation_doc (id, user_id, body, body_hash, created_at)`.
- [ ] A2. **`logic_check` re-judgment** — the `UNIQUE (user_id, commitment_id)` table
      constraint blocks a second verdict, so 0031 rebuilds the table (create/copy/drop/
      rename) with a *partial* unique index `WHERE status != 'superseded'` and adds
      `superseded_by`. Verdicts supersede, they never UPDATE — house style, and the
      history is what makes a re-judgment auditable rather than a row that changed its
      mind silently.
- [ ] A3. **`backglass/situation.py`** — `render(conn, settings, day) -> str`: who and
      where (identity/housing/enrolment facts), the current phase, the active fronts,
      and **what recently changed** (facts superseded or retracted in the window, with
      both values). Every line carries `[fact N]` / `[commitment N]`, so rule 1 holds
      inside the doc and a dependency can point at a claim that has provenance.
      `save()` writes a new version only when `body_hash` differs — rule 3, and the
      version list is the evolution the owner asked to see.
- [ ] A4. **The relevance pass records dependencies, not just drops.** Extend
      `RelevanceVerdict` with `depends_on: list[int]` (fact ids) and require it on
      *every* verdict including `keep` — same call, same batch, richer output. The
      existing `cites_fact` stays as the drop citation; it is the degenerate
      single-dependency case. Same three guards: ids intersected with what was sent,
      facts intersected with the active set, quote checked against the source item.
      `check-relevance.md` → version 2, fixtures for the new field, no live API.
- [ ] A5. **The checker reads the situation.** `render()` feeds the relevance prompt
      alongside the raw addressable facts — the model currently sees atomic facts and
      not the shape of the week. Input cost is nearly free here (2026-08-07 audit), so
      this is not pre-optimised away.
- [ ] A6. **Surfaces** — `uv run backglass situation` (and `--json`), plus the doc and
      its version diff on the Memory page. Named `situation`, not `state`: `backglass
      state` already means installation ground truth and the collision would be cruel.

### Increment B — invalidation, dry run only. Nothing writes.

- [ ] B1. **`logic.py` gains `_dependency_no_longer_holds`** — an open commitment whose
      active dependency names a fact that is now `superseded` or `retracted`. This is a
      positive contradiction and belongs in that file: the ledger holds the row that
      contradicts the row being closed, and the rule can print both values.
- [ ] B2. **Targeted re-judgment** — a broken dependency supersedes its `logic_check`
      verdict and re-queues that commitment, and *only* that commitment. Keyed on
      (commitment, fact version), so an unchanged ledger re-judges nothing and writes
      nothing on the second pass. Rule 3 asserted on real data, not fixtures.
- [ ] B3. **The would-drop list, printed and classified row by row.** Three read-only
      runs against the live ledger, zero writes, every row read and classified before
      anything is believed. The 2026-08-20 lesson is about this exact shape of mechanism
      coming within one clean run of retracting sixteen live classes, and it says
      explicitly that a passing suite is not evidence — both defects passed all 32 tests.

### Increment C — enable, after the owner has read the list.

- [ ] C1. **Drop or ask, asymmetric by consequence.** At or above the drop threshold the
      commitment is tombstoned citing the dependency and the fact that superseded it;
      below it, one question, nothing dropped. Inherited from `relevance.py` unchanged,
      because a wrong keep costs a click and a wrong drop is silent.
- [ ] C2. **Backfill** — ~226 open commitments through the dependency pass, several syncs'
      work at `PER_RUN = 75`. A pass of this size measured ~$6.50 on 2026-08-19; the
      spend cap (rule 7) is a hard stop and this must degrade into it, not around it.
- [ ] C3. Wire into `sync.py` behind the same gate the relevance pass uses.

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

- [ ] B1. A feed due date that differs from the commitment's moves the commitment, with
      the old value and the feed's own read time in the note, plus a `deadline-moved`
      notification through notify's dedup. This is positive contradiction — the upstream
      record disagrees with ours — not silence, so it belongs with `logic.py`'s rules.
- [ ] B2. An assignment in the ledger and no longer in the feed is `retraction`'s shape,
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

- [ ] D1. `assignment-effort@1`, shaped exactly like `check-relevance`: bounded slice per
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
- [ ] Rebuild the desktop sidecar, or say plainly that it is behind.

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


## Goal 4 (2026-08-24): the operational commands leave the terminal

Owner's directive: "all slash commands should be triggered with gui and never have the
user touch the terminal." Confirmed with them as the Backglass dashboard rather than
Claude Code's own slash commands, and scoped to the safe operational set rather than all
74 CLI verbs.

**The gap, read rather than assumed.** The dashboard already covers the *data* half:
fourteen route modules, and per-row actions for answering, accepting, ticking, merging,
resolving. What has no GUI at all is the *operational* half — the verbs that make the app
run. `sync`, `loop`, `logic`, `relevance`, `recheck`, `duplicates`, `index`, `plan`,
`brief`, `state`, `doctor`, `status`, `backup`, `notifications`, `context`. Every one of
them is a terminal command today, which is why goal 3 kept finding surfaces the owner
could not reach.

**Decided with the owner:** destructive verbs stay terminal-only. `purge-boundary`,
`prune`, `restore` and `scrub` carry data-loss or legal weight (docs/08), and putting them
one fuzzy-search from a mis-click buys nothing the terminal does not already give.

### The shape

- **The palette calls the library, not the CLI.** Every command in the set already exists
  as a function the CLI wraps — `sync.sync`, `loop.run`, `logic.run`, `backup.run`,
  `state.collect`. The palette calls the same function. No subprocess, no `uv run` path to
  get wrong, no stdout scraping, and no second implementation that can drift from the
  first. A registry entry is a name, a sentence, and a callable.
- **One job at a time, on a daemon thread.** These take minutes and take `run_lock`; a
  request thread must never wait on one. The page starts a job and polls. `run_lock`
  already makes a second concurrent run skip rather than corrupt, so the in-process guard
  is about not lying to the owner, not about safety.
- **Nothing arbitrary.** The route takes a registry key, never a command string. There is
  no path from the browser to a shell.
- **The record is the ledger's, not the palette's.** A job's output is transient and lives
  in memory; what a run *did* is already written by `loop_pass`, `run` and `decision`. No
  migration.

### Increments

- [x] 1. `backglass/web/commands.py` — the registry: name, one-line description, callable,
      whether it takes the lock. Plus `jobs.py`: start one, poll it, read its lines, with
      the same rule-5 stance as the loop (a job that raises is a recorded failure, not a
      dead page).
      landed: nine commands — loop, logic, questions, duplicates, noise, notifications,
      index, state, backup. `Result` carries the command's own lines and its own `ok`,
      kept separate from the job's `status`: a degraded sync RAN and is reporting
      something true, and merging the two would render a crash as bad news or bad news as
      a crash depending on which way the merge went.
- [x] 2. Routes and the palette itself — ⌘K overlay, fuzzy filter, Enter to run, output
      streamed by HTMX polling. Design system applies: cream and black, no shadows or
      gradients, `--radius-2` on the controls and `--radius-3` on the container, the
      keyline carrying the colour.
      landed: ⌘K overlay in base.html, substring filter over key+label+blurb (not fuzzy —
      nine commands do not need ranking, and a fuzzy matcher is how a destructive verb
      ends up one keystroke away), arrow keys, Enter runs and the palette STAYS open
      because the output lands in it. Three routes; the run route takes a registry key and
      can express nothing else. 13 tests.
      Two design-system tests caught this rather than review: an inset `box-shadow` used
      as a selection keyline (the decisions table forbids shadows — now a transparent
      border that cannot shift the text), and `#cmdrun` snapping in beside fragments that
      fade. It fades without a translate, because it re-renders every 700ms while a
      command runs and a translate at that cadence reads as a fault.
- [ ] 3. The commands that need arguments (`plan <date>`, `brief <date>`, `context`) get
      the smallest input that works, or are left out of increment 1 rather than guessed at.
- [x] 4. Verify against a copy of the live ledger, not fixtures — goal 3's own lesson: the
      noise card's wording defect was invisible until it rendered real data.

### Constraint carried from goal 3

Every claim the palette prints about what a command did comes from the command's own
report. A palette that says "sync complete" when the report says `degraded` would be the
first surface in this app to invent one.
