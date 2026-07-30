> **STALE — do not act on §0.** This file was written by a parallel session at 10:51 on
> 2026-07-30, while the build it describes was mid-flight. Its "Where the build actually
> is" table was wrong on four of five rows within the hour: the suite is green (232
> tests), the CLI works, `migrate()` is fixed, fixtures and evals exist. The accurate,
> maintained log is [`tasks/todo.md`](todo.md). Kept for its phase-arc thinking, not its
> status claims.

# Backglass — full implementation plan

Master plan across all phases, with AI spend engineered down as a first-class constraint
rather than watched after the fact. `tasks/todo.md` stays the active Phase 1 session
checklist; this file is the arc.

Phase numbering follows `docs/09-build-plan.md` and does not renumber it. Two phases are
inserted (1B, 6) because the cost work and the ops work do not belong inside any existing
phase.

---

## 0. Where the build actually is

Verified 2026-07-30 by running the thing, not by reading it.

| Claim | Reality |
|---|---|
| Phase 1 code | Written. `connectors/`, `extract/`, `ledger.py`, `sync.py`, migrations, queries — ~2,900 lines |
| Phase 1 green | **No.** The suite errors on the first fixture |
| `backglass` CLI | **Broken.** `ModuleNotFoundError: No module named 'backglass'` — stale venv install, `pyproject.toml` itself is correct |
| `migrate()` | **Bug.** `db/__init__.py:120-131` opens `BEGIN`, then `executescript()` issues an implicit COMMIT, so the explicit `COMMIT` raises `cannot commit - no transaction is active`, and the `ROLLBACK` in the handler raises over the top of it |
| Fixtures | `tests/fixtures/{gmail,commitments,model}/` all empty |
| Evals | `evals/` empty |
| `vcrpy` | Required by `tasks/todo.md` §Session 2, absent from `[dependency-groups].dev` |
| Phase 0 | Decisions made (boundary Option B, mailboxes, spend cap). The fourteen hand-built briefs are **not** evidenced in the repo |

**Assumption, stated rather than buried:** Phase 0's exit criterion (brief opened ≥10 of
14 mornings) was satisfied outside the repo, because Phase 1 was built. If it was not,
stop and do Phase 0 — it is two days and it gates five weeks.

---

## 1. Cost architecture

The rest of this plan is ordinary engineering. This section is the part that answers the
actual question, so it comes first and every phase below references it.

### 1.1 The load-bearing idea

**AI touches exactly two operations in the entire product: triage and extraction. Nothing
else may call a model, ever.**

The brief is a template render over SQL rows. The day planner is an interval-packing
algorithm. Goals, staleness, risk, capacity, rollover, streaks — arithmetic. The
dashboard is Jinja2. If a phase below appears to need a model, that is a design error in
that phase, not a budget request.

This is already implied by `docs/02` ("reports over those records") but is nowhere
written as a prohibition, so it gets violated the first time a brief section reads
awkwardly and generating it sounds easier than templating it. It is not easier. It is a
recurring daily cost, a nondeterministic output on a surface whose whole value is
trustworthiness, and a violation of rule 1 (every claim links to its source — generated
prose has no source).

### 1.2 Baseline: what it costs today

Assumptions, all of them adjustable and none of them measured yet:

- 120 inbound messages/day across `contactdharsan@gmail.com` + `dkesava2@asu.edu`
- Current tier-0 rules kill ~75% → 30/day reach the triage model
- Triage keeps ~20% → 6/day reach extraction
- Measured triage cost with the mandatory isolation flags: **$0.0031/call**
- Extraction on `sonnet`, ~3k in / 300 out: **~$0.012/call** (estimate, unmeasured)

```
triage      30/day × $0.0031  = $0.093/day  = $2.79/mo
extraction   6/day × $0.012   = $0.072/day  = $2.16/mo
                                              ───────
                                              ~$5/mo
```

Under the $20 cap, so nothing breaks. It is also ~10× more than this workload needs, and
the number that matters is not the monthly bill — it is that **iterating on a prompt
costs $5 a run**, which is what actually makes people stop iterating.

### 1.3 The eight layers, cheapest first

Ordered by cost-per-unit-of-work-avoided. Each one is a phase-1B deliverable.

**L0 — Response cache.** A `model_cache` table keyed by
`(prompt_stamp, model, sha256(rendered_input))` → response JSON + cost. Checked before
every call, written after every success. Costs one table and ~40 lines.

Effect on the monthly bill: roughly zero. Effect on the build: it is the single highest
-value item in this document, because re-running the pipeline while debugging
post-processing goes from $5 to $0. Every prompt iteration, every idempotency test run,
every "what happens if I change the dedup threshold" is free after the first pass. Build
it first, before the levers that actually reduce steady-state spend.

**L1 — Learned noise senders.** Tier-0 already supports `noise_senders` but `sync.py:174`
never passes it — the parameter is dead. Populate it from the ledger:

```sql
-- a domain whose last 20 triaged items were all 'drop' is noise
SELECT domain FROM ... GROUP BY domain
HAVING COUNT(*) >= 20 AND SUM(verdict = 'keep') = 0
```

Recomputed nightly, written to a `noise_sender` table, loaded at sync start. Pure SQL,
zero model calls, and it converges on the owner's actual inbox rather than a guessed list.
Expected tier-0 kill rate: 75% → ~88%.

**L2 — Recall-safe lexical gate (tier 0.5).** Before spending a model call, check whether
the message contains any commitment-shaped signal at all: a modal or promissory token
(`will`, `I'll`, `can you`, `could you`, `please`, `need`, `by`, `deadline`, `send`,
`review`, `confirm`), a date expression, or a question mark. No signal → drop with
`triage_reason = 'no commitment signal'`.

`triage.md` is explicit that a false negative loses a commitment permanently, so this gate
is **tuned for recall, not precision**: it ships only once it scores ≥99% recall against
the labelled fixture set from L7, and its recall is re-measured on every prompt version
bump. If it cannot hit 99%, it does not ship. That constraint is the whole reason this is
a gate and not a classifier.

**L3 — Batched triage.** The dominant cost in a triage call is not the message, it is the
fixed overhead: system prompt, schema, CLI process, model warm-up. One message per call
pays that overhead 30 times a day.

Pack N=20 messages into one call, each truncated, returning an array of
`{index, keep, reason}`. Overhead amortizes ~20×.

```
30/day, 1-per-call:  30 × $0.0031  = $0.093/day
14/day, 20-per-call:  1 × ~$0.006  = $0.006/day
```

Two rules that keep this honest: the batch is order-preserving and index-checked (a
response whose indices do not match the request is a `ModelError`, not a best-effort
mapping), and a batch that fails falls back to per-item calls for that batch only, so one
bad message cannot lose nineteen good verdicts.

**L4 — Input slimming.** Cheap and immediate. Before either tier:

- strip quoted history (already done in the Gmail connector — verify it, do not assume)
- strip signature blocks, legal disclaimers, unsubscribe footers
- strip URLs to their bare domain (a tracking URL is 200 tokens of zero commitment signal)
- collapse whitespace runs
- triage body limit `2000 → 800` chars. Keep/drop lives in the subject and first
  paragraph; carrying 2000 chars buys nothing and is measurable in the bill

Expect 40–60% token reduction on real mail, which is mostly HTML-derived text.

**L5 — Escalating extraction.** `MODEL_EXTRACT=sonnet` for everything is the wrong shape.
Most extractions are one obvious commitment in a short message.

Run `haiku` first. Escalate to `sonnet` only when: `haiku` returns any commitment below
`confidence_threshold`, or returns ≥3 commitments, or the input exceeds 4k chars. Record
which model produced each row (`commitment.model` column) so the escalation rate is
measurable and the eval can compare the two tiers on the same fixtures.

If the escalation rate exceeds ~30%, haiku-first is not paying for itself and the config
flips back — that is a measurement, not an argument.

**L6 — Per-stage budgets.** The cap in `sync.py` is global, so a runaway triage pass can
consume the whole month and starve extraction, which is exactly backwards: extraction is
the tier where money is worth spending. Split it — triage gets ≤25% of the monthly cap,
extraction the rest. Triage exhausting its share degrades triage to rules-only, not the
whole pipeline.

**L7 — Fixture-replay mode.** `--replay` on `sync` and `extract`, backed by
`tests/fixtures/model/`. Runs the entire pipeline with a `ReplayBackend` that raises on a
cache miss rather than calling out. This is what makes it *impossible* to accidentally
spend money while working on post-processing, as opposed to merely unlikely.

### 1.4 Target

Same assumptions as §1.2, with L0–L6 applied:

```
tier-0 + L1 + L2      120/day → ~9/day reach the triage model
L3 batched triage     1 call/day        ~$0.006/day  = $0.18/mo
L5 extraction         6/day, 85% haiku  ~$0.015/day  = $0.45/mo
                                                       ───────
                                                       ~$0.65/mo
development re-runs   L0 + L7                           $0.00
```

**~$0.65/month steady state, $0 to iterate.** Estimate, not measurement — the numbers
that matter are `run.spend_cents` and the triage kill rate, both of which land in the
Sources panel in Phase 3, at which point every figure in this section gets replaced by a
real one.

Set `MONTHLY_SPEND_CAP_CENTS=300` once L0–L6 land. A $20 cap on a $0.65 workload is not a
cap, it is a rounding error with a nice name.

### 1.5 The endgame, deliberately deferred

Triage is a binary classification over short text. A local 4B model on the owner's Mac
does it at $0.00 and ~200ms, and would take the steady-state bill to extraction-only,
call it $0.45/mo.

Deferred to Phase 6 and optional, for two reasons: the `ModelClient` protocol already
makes it a backend swap rather than a rewrite, so there is no architectural cost to
waiting; and this machine reports **99% disk usage with 4.9 GiB free**, which is not
enough headroom for model weights. Free up disk before considering it.

---

## Phase 1A — Close out Phase 1

**Half a day.** Phase 1 is written and has never run green. Nothing else starts until it
does. `docs/09`: do not start a phase until the previous one runs clean twice.

- [ ] `uv sync` — fix the broken console script. Confirm `uv run backglass --help` lists
      every command in `docs/10` §CLI
- [ ] Add `vcrpy` to `[dependency-groups].dev` (required by `todo.md` §Session 2, missing)
- [ ] **Fix `migrate()`** — `db/__init__.py:120-131`. `executescript()` implicitly commits,
      so the surrounding `BEGIN`/`COMMIT` is incoherent and the `except` handler's
      `ROLLBACK` masks the real error with a second one. Either drop the explicit
      transaction and let `executescript` own it, or split the migration into statements
      and execute them individually inside the transaction. Prefer the second: a migration
      that half-applies is the failure mode `schema_version` exists to prevent
- [ ] `tests/test_migrations.py` — init is idempotent; a file-set/recorded-version mismatch
      refuses to start; a half-applied migration leaves no `schema_version` row
- [ ] Record `tests/fixtures/gmail/` cassettes with `vcrpy`, both mailboxes, first run and
      incremental. No test hits a live API
- [ ] Hand-write `tests/fixtures/model/` responses for triage and extraction — readable
      JSON, per `docs/10` §Testing
- [ ] `tests/fixtures/commitments/` — all seven cases from
      `specs/extraction-prompts/extract-commitments.md`
- [ ] `tests/test_idempotency.py` — sync twice over frozen fixtures, assert the second run
      writes zero domain rows (`writes` excludes `run`, per `todo.md` §Deviations #7)
- [ ] `tests/test_dates.py` — confirm it covers both directions of the Phoenix↔Kolkata move
      and a three-week-old relative date, per rule 4
- [ ] `evals/eval_triage.py`, `evals/eval_commitments.py` — real calls, precision/recall per
      prompt version, never in CI
- [ ] Resolve `todo.md` §Deviations #2 (`credential` is `UNIQUE(user_id, source)`; two Gmail
      accounts need `gmail:personal` / `gmail:asu`) — it blocks two-mailbox ingest
- [ ] `backglass sync --dry-run` against real mail, then a real run

**Exit:** `uv run pytest` green twice in a row, and a SQL query over the real db returns
the owner's actual open commitments, correctly.

**AI spend this phase:** ~$5 for the first real run, ~$0 thereafter (fixtures).

---

## Phase 1B — The cost layer

**Two days.** Before Phase 2, because every later phase re-runs the pipeline and each
re-run is currently $5. Building this second pays for itself inside a week.

- [ ] **L0** `model_cache` table + migration `0003_model_cache.sql`; check-before-call,
      write-after-success in `extract/client.py`. Keyed on `(prompt_stamp, model,
      sha256(input))` so a prompt version bump correctly invalidates
- [ ] **L7** `ReplayBackend` + `--replay` on `sync` and `extract`. Raises on cache miss
- [ ] **L4** `extract/slim.py` — quoted history, signatures, disclaimers, URL→domain,
      whitespace. Unit-tested against ten real-shaped messages. Verify the Gmail connector's
      existing quoted-history stripping actually fires before hashing rather than assuming it
- [ ] **L4** `BODY_LIMIT` 2000 → 800 in `extract/triage.py`, with the eval re-run to confirm
      recall did not move
- [ ] **L1** `noise_sender` table, nightly recompute query, wired into
      `rules.classify(noise_senders=...)` — the parameter exists and `sync.py:174` never
      passes it, so tier-0's cheapest rule is currently dead code
- [ ] **L2** `extract/signal.py` lexical gate. Ships only at ≥99% recall on the labelled
      fixture set. Recall assertion lives in the eval, not the test suite
- [ ] **L3** batched triage: `TriageBatch` schema, index-checked responses, per-item
      fallback on batch failure, N configurable (`TRIAGE_BATCH_SIZE=20`)
- [ ] **L5** haiku-first extraction with escalation; `commitment.model` column; escalation
      rate in the run report
- [ ] **L6** per-stage budget split in `SpendCap`; triage exhaustion degrades triage only
- [ ] Stage-level cost breakdown on `run` (`spend_triage_cents`, `spend_extract_cents`,
      `cache_hits`) — you cannot tune what you cannot attribute
- [ ] `MONTHLY_SPEND_CAP_CENTS=300`

**Exit:** one week of real syncs at **< $0.25 total**, triage kill rate ≥ 88%, lexical gate
recall ≥ 99% on fixtures, and a full pipeline re-run under `--replay` costing $0.

---

## Phase 2 — Morning brief

**Three days.** `docs/05`. **Zero model calls.** Every section is a SQL query and a Jinja2
partial.

- [ ] `brief/daily.py` — one query per section, precedence-deduplicated (B4: no item in two
      sections, precedence = section order)
- [ ] `brief/render.py` — inline-styled table layout, cream `#FAF3DF` set on the outer
      table, black rules for section breaks, five inks with black keylines. Plaintext
      alternative generated from the same data, never by stripping tags
- [ ] B1 word-count enforcement in code: truncate the lowest-priority section and say so
- [ ] B2 provenance: a line without a source link does not render. Assert this in a test,
      not in review
- [ ] B3 empty sections omitted entirely
- [ ] B5 ledger-only: assert the brief path opens no connector and constructs no
      `ModelClient`. A test that fails if `brief` imports `extract.client` is cheap and is
      the enforcement mechanism for §1.1
- [ ] B6 failure notice on generation failure — short, not silence
- [ ] B7 `brief.opened_at` via tracking pixel + link click
- [ ] Failure states: source failing >1 cycle named at the top; degraded pipeline said
      plainly
- [ ] Resend or Postmark delivery. Never the Gmail API — `gmail.readonly` and nothing else
- [ ] Render checks in Gmail web, Gmail iOS, Apple Mail, Outlook
- [ ] `com.cognifer.backglass.brief.plist` at 06:00 local. launchd, never an in-process
      scheduler

**Exit:** seven consecutive briefs, each < 400 words, every line's source link resolving,
zero wrong claims. Run it a full week before starting Phase 3 — `docs/09` is right that the
urge to build through this week is strong and wrong.

**AI spend delta: $0.00.**

---

## Phase 3 — Dashboard

**One week.** `docs/06`. **Zero model calls.** FastAPI + Jinja2 + HTMX, no build step.

Read-only first:

- [ ] `web/app.py`, one page, panels: Today, Commitments, Awaiting, Review queue, Sources
- [ ] `design/tokens.css` + hand-written rules. Match `design/preview.html`. No shadows, no
      rounded corners except the 2px reel digit windows, black keyline on every fill, three
      chart series maximum, tabular figures
- [ ] Empty states, declarative, per `docs/06` §Empty states. Never "You're all caught up 🎉"
- [ ] **Sources panel on day one**, not at the end: last successful sync per source, item
      counts, triage kill rate, failures with vermilion keylines that persist until fixed

Then write-back — the part that decides whether this gets opened in week two:

- [ ] Resolve / drop / snooze a commitment
- [ ] Accept / reject a review-queue item
- [ ] Tick / untick a checklist item (lands in Phase 4; wire the endpoint here)
- [ ] Mark a plan block done or rolled; pin a block; edit an effort estimate; adjust a
      weekly target
- [ ] Optimistic UI with rollback. Confirmation dialog on `drop` only
- [ ] Keyboard: `j`/`k`, `x` to resolve, `r` for the review queue

**Cost instrumentation lands here** — the Sources panel is where §1.4's estimates get
replaced by measurements. Add: spend this month against cap, cache hit rate, escalation
rate, cost per extracted commitment.

**Exit:** resolving a commitment in the dashboard removes it from the next brief, and a
source auth failure is visible in the panel within one cycle.

**AI spend delta: $0.00.**

---

## Phase 4 — Schedule and goals

**One and a half weeks.** `docs/04`, the largest phase. **Zero model calls** — every
requirement here is arithmetic over ledger rows.

Order within the phase is `docs/09`'s and should not be rearranged; capacity has to be
right before anything consumes it.

1. **Capacity** — `plan/capacity.py`. Working window − fixed − buffer − travel − reserve.
   Reserve never zero (default 45 min). P1–P3.
2. **Effort estimates** — implement `todo.md` §Deviations #5: type defaults (review 30,
   draft 60, decision 15, meeting-prep 30, unknown 45), sticky owner overrides,
   `estimate_source` recorded. Extraction already proposes an estimate where the text
   supports one; this is the fallback layer, and it is code, not a model call.
3. **Day planner** — `plan/planner.py`. Selection, ordering (§1.5 precedence), protected
   ≥90min block in the peak window (P6–P9), 25-minute block minimum with a "small items"
   batch (P4), never across a fixed event (P5), explicit overflow reporting (P2).
4. **Rollover** — `plan/rollover.py`. P10–P12, third-rollover question exactly once.
5. **Evening shutdown** — three fields, prefilled, skippable. Infer from ledger state when
   skipped. Never nag.
6. **Timezone** — P13–P16. Active zone from explicit setting with optional date range, never
   IP. Both-zone display on cross-zone meetings. This is the acceptance criterion most
   likely to be quietly wrong; it gets its own test file per `docs/10`.
7. **Goals** — `goals/targets.py`, `goals/checkpoints.py`. Entered by hand, never inferred
   (`docs/04` §6). G1–G10. At most one goal per commitment.
8. **Staleness and risk** — `goals/staleness.py`, `goals/risk.py`, computed **independently**,
   never merged into a health score. G11–G14.
9. **Checklist** — 7-item cap enforced not advisory, binary, weekday-scoped, quiet streak
   reset. C1–C6.
10. **Monday planning / Friday retro** — `brief/monday.py`, `brief/friday.py`. Monday
    *replaces* the brief (W1). Friday appends, ≤150 words (W2). Both ledger-generated (W3).
11. **Capacity check** — G5–G7, committed hours against available, named gap, no auto-drop.

Schema: `goal`, `target`, `checkpoint`, `checklist_item`, `checklist_tick`, `day_plan`,
`plan_block`, `shutdown_note`. `docs/04` §3 has the DDL; check it against `specs/schema.sql`
before writing a migration — if it is already there, this is a no-op.

One extraction change touches AI: `extract-goal-signal.md` already exists and is unused.
Wire it as a **field on the existing extraction call**, not a third call. Goal linkage is
one more key in the same JSON response. A separate pass would double extraction spend for
one boolean.

**Exit:** every acceptance criterion in `docs/04` §5, including Phoenix→Coimbatore.

**AI spend delta: $0.00** (goal signal rides the existing extraction call).

---

## Phase 5 — Widen intake

**One week.** Late and cheap because the extraction pipeline already works. Each connector
is scoped so that widening intake does not multiply spend.

- [ ] **Calendar** (Google Calendar, Microsoft Graph if the work tenant is enabled).
      **Zero AI** — events are already structured. Read-only; the planner proposes and never
      writes. Declined excluded from capacity, tentative counts as busy, travel tagged per
      `docs/04` §1.2
- [ ] **Canvas** (optional, the request that seeded the project). **Zero AI** — assignments
      and due dates are structured fields. `Link`-header pagination to completion, serialized
      requests, back off on 403. Check Approved Integrations before building; fall back to
      the ICS feed if student tokens are disabled
- [ ] **Obsidian notes.** Vault directory watch, no API, no auth — a tenth the work of Notion
      or Apple Notes. Extract **only changed sections of changed files**, diffed against the
      stored hash. Re-extracting a whole vault on every sync is the one way this phase
      becomes expensive, and a section-level diff prevents it structurally
- [ ] **Drive.** Changes feed, same cursor pattern. Docs/Sheets/PDFs only, binary and media
      skipped. Owned by or explicitly shared with the owner — do not crawl shared drives.
      Cap per-document extraction to the first N characters plus headings; a 40-page PDF
      does not carry 40 pages of commitments
- [ ] Every connector runs through `boundary.py` **before it yields**, per `docs/08` and
      `docs/10` §Layout
- [ ] Every connector reports `Health`; failure degrades and never blocks (rule 5)

**Exit:** every source healthy in the Sources panel for seven consecutive days, and the
triage kill rate above 85%.

**AI spend delta:** the point of the scoping above is that this stays under ~$0.30/mo even
with five sources. Watch the kill rate — if it drops below 85% after adding a source, that
source's rules are too permissive and the cost model is about to break (`docs/02`).

---

## Phase 6 — Ops and the local tier

**Ongoing.** Not a build phase; the things that keep it alive.

- [ ] **Backup.** The db is one file. `cp` it on a schedule, off the machine. There is no
      other copy of the ledger
- [ ] **launchd health.** A job that fails to fire is invisible to a Sources panel that only
      knows about connectors. Add last-run age to the panel: no successful run in 90 minutes
      is itself a failure state
- [ ] **Monthly cost review.** Cost per extracted commitment, cache hit rate, escalation
      rate, kill rate. Four numbers, one query
- [ ] **Prompt version discipline.** Bumping a version re-extracts. Always `--dry-run` the
      diff first; `source_item` immutability is what makes this safe (`docs/02`)
- [ ] **Optional — local triage model.** `OllamaBackend` behind the existing `ModelClient`
      protocol takes triage to $0.00. Blocked on disk: this machine is at 99% used, 4.9 GiB
      free. Free space first, and treat the eval as the gate — a local model that drops
      recall below the L2 threshold is not cheaper, it is broken
- [ ] **Optional — GitHub Actions** if the laptop sleeps through 06:00 regularly

---

## Cost ledger

| Phase | Adds AI cost? | Steady state after |
|---|---|---|
| 1A close-out | one real run, then fixtures | ~$5/mo |
| 1B cost layer | **removes it** | ~$0.65/mo, $0 to iterate |
| 2 brief | no | ~$0.65/mo |
| 3 dashboard | no | ~$0.65/mo |
| 4 schedule + goals | no (goal signal rides extraction) | ~$0.65/mo |
| 5 widen intake | yes, bounded by scoping | ~$0.95/mo |
| 6 local triage (optional) | **removes it** | ~$0.45/mo |

Every figure past 1A is an estimate from §1.2's assumptions. They get replaced by
`run.spend_cents` once Phase 3's Sources panel exists.

---

## The rules that keep it cheap

Additions to `CLAUDE.md` §Rules that are load-bearing, in the same spirit — violating any
of these breaks the cost model rather than the product.

8. **Only triage and extraction call a model.** No other module may import
   `extract.client`. Briefs, plans, goals, and the dashboard are deterministic functions of
   ledger state. Enforced by a test, not by intent.
9. **Every model call goes through the cache.** A code path that calls a backend directly is
   a bug, not an optimization.
10. **Never call a model for something a rule can decide.** A rule is free, deterministic,
    auditable, and testable. Reach for the model only after the rule layer has genuinely
    failed to classify.
11. **A recall gate ships on a measured recall number or it does not ship.** L2 is the only
    layer here that can lose data, and `triage.md` is right that the loss is silent and
    permanent.
12. **Cost is attributed per stage, not aggregated.** An unattributed bill cannot be tuned.

---

## Open questions

Each needs a ruling; none block Phase 1A.

1. **Phase 0's fourteen briefs** — done outside the repo, or skipped? If skipped, the
   two-day gate is worth going back for.
2. **`credential` UNIQUE(user_id, source)** with two Gmail accounts (`todo.md` §Deviations
   #2). Proposal on the table is `gmail:personal` / `gmail:asu` with the connector name
   parsed from the prefix, no schema change. Needs a yes before two-mailbox ingest.
3. **Email provider** — Resend or Postmark. Either works; pick one in Phase 2.
4. **Notes app** — `docs/07` recommends Obsidian and `docs/09` §Phase 0 says decide it in
   Phase 0. Not recorded anywhere in the repo.
5. **`TRIAGE_BATCH_SIZE`** starting value. 20 is a guess; the first week of real data
   settles it.
