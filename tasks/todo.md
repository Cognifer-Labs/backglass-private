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
- [ ] 6. **Duplicates leaves the terminal.** The clustered review becomes a loop pass that
      raises cards on the page where `same`/`distinct` already live, bounded per refresh
      like staleness's five. Auto-collapse stays owner-gated — a row that vanishes with
      nothing saying why is the failure four lessons already cover. Duplicate engagements
      (`plan_clusters`) get their first surface.
- [ ] 7. **Owner question, not a flip.** `noise_auto_promote` is off by default and domain
      candidates are CLI-only by design. Raised as a question on the page; the default is
      not changed silently.
- [ ] 8. **Triage efficiency: measure before touching.** Batch triage and parallel passes
      already exist. Read `model_call` for where latency and money actually go, and only
      then decide whether anything in triage is worth reworking. No rewrite on a hunch.

### Constraints that bite (carried forward)

- Migration 0030 re-arms the frozen-sidecar crash; the rebuild is part of increment 2.
- No new scheduler. launchd owns cadence.
- No pass may become load-bearing on retrieval (CLAUDE.md's additive ruling).
