# Build Plan

Six phases. Do not start one until the previous runs clean twice in a row.

---

## Phase 0 — Validate without building

**Two days. No code. This phase gates everything else.**

Personal knowledge tools fail with remarkable consistency in the same way: heavy build,
two weeks of enthusiastic use, silence. Phase 0 tests for that before there is anything
to abandon.

Generate the morning brief by hand, or with a scheduled assistant task using existing
email and document connectors, every morning for two weeks. No database, no pipeline, no
extraction code.

**Exit criterion:** the brief was opened on at least ten of fourteen mornings and at
least one item surfaced that would otherwise have been missed.

If it was opened four times and then ignored, that is the finding. Two days spent instead
of two months.

Also decide, in this phase and not later:

- The data boundary decision in `docs/08-privacy-and-data-boundary.md`.
- Which notes app (`docs/07-connectors.md` recommends Obsidian).
- Working window, peak window, and week start.

---

## Phase 1 — Ledger and Gmail extraction

**One week.**

- Schema from `specs/schema.sql`.
- Gmail connector, incremental, with the data boundary enforced in the connector.
- Two-tier extraction: rules, then triage model, then extraction model.
- Commitments only. No goals, no plans, no brief, no dashboard.

**Exit criterion:** a SQL query returns the owner's actual open commitments, correctly,
and a second run over unchanged input writes nothing.

Build the dry-run mode and the idempotency test first, not last. They are what make
every later phase debuggable.

---

## Phase 2 — Morning brief

**Three days.**

- Generation from ledger state, per `docs/05-morning-brief.md`.
- Email delivery with inline styles, tested in four clients.
- Source links on every line.
- `brief.opened_at` tracking.

**Exit criterion:** seven consecutive daily briefs, each under 400 words, each with
working source links, none containing a claim that turned out to be wrong.

Run it for a full week before adding anything. The temptation to keep building through
this week is strong and should be resisted, because this is the first point where real
feedback exists.

---

## Phase 3 — Dashboard

**One week.**

- Read-only first: Today, Commitments, Awaiting, Review queue, Sources.
- Then write-back: resolve, accept, reject, tick.
- Sources panel from day one, not at the end.

**Exit criterion:** resolving a commitment in the dashboard removes it from the next
brief, and a source auth failure is visible in the panel within one cycle.

---

## Phase 4 — Schedule and goals

**One and a half weeks.** The largest phase, specified in full in
`docs/04-daily-schedule-and-goals.md`.

Order within the phase:

1. Capacity model and effort estimates. Get capacity right before anything consumes it.
2. Day planner: selection, ordering, protected block, overflow reporting.
3. Rollover and the evening shutdown pass.
4. Goals, targets, checkpoints.
5. Staleness and risk, computed independently.
6. Checklist.
7. Monday planning and Friday retro.

**Exit criterion:** the acceptance criteria in
`docs/04-daily-schedule-and-goals.md` §5, all of them, including the Phoenix-to-Coimbatore
timezone case.

---

## Phase 5 — Widen intake

**One week.**

Drive, notes, calendar, Canvas. Each is a connector against an extraction pipeline that
already works, which is why this is late and cheap rather than early and expensive.

**Exit criterion:** every source reports healthy in the Sources panel for seven
consecutive days, and triage kill rate is above 85 percent.

---

## Totals

Roughly five to six weeks of focused effort. For an agent-assisted build with diff
review, probably three.

Phase 0 gates all of it, and phase 0 is two days.

---

## Milestone checklist

```
[ ] P0  Fourteen hand-built briefs, opened ≥10 times
[ ] P0  Data boundary decided and written into config
[ ] P0  Notes app chosen
[ ] P1  Schema created, migrations runnable
[ ] P1  Gmail incremental ingest with boundary enforcement
[ ] P1  Triage rules + model, kill rate measured
[ ] P1  Commitment extraction with confidence scores
[ ] P1  Dry-run mode
[ ] P1  Idempotency test passing
[ ] P2  Brief generation under 400 words
[ ] P2  Email rendering verified in 4 clients
[ ] P2  Source links on every line
[ ] P2  Seven consecutive briefs, zero wrong claims
[ ] P3  Dashboard read-only, all panels
[ ] P3  Sources panel with triage kill rate
[ ] P3  Write-back on all seven actions
[ ] P4  Capacity model with reserve
[ ] P4  Day planner with protected block
[ ] P4  Rollover + third-rollover question
[ ] P4  Timezone change handling, both directions
[ ] P4  Goals, targets, checkpoints
[ ] P4  Staleness and risk computed independently
[ ] P4  Checklist with 7-item cap
[ ] P4  Monday planning replaces brief; Friday retro appends
[ ] P5  Drive, notes, calendar connectors
[ ] P5  Canvas connector (optional)
[ ] P5  Seven days all-healthy
```
