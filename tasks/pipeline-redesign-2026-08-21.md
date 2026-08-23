# A general pipeline, designed blind, compared to what exists — 2026-08-21

Companion to `tasks/pipeline-audit-2026-08-21.md` (defect-level audit, same day). This one
is architectural: design the pipeline from the purpose alone, then hold the current schema
and module set against it and see where they diverge structurally — not bug by bug.

**Rules of the comparison, stated first so the doc cannot be misread:**

- The greenfield design is a **measuring stick, not a rewrite pitch**. CLAUDE.md's
  decisions-already-made table stays closed. What the comparison extracts is *machinery* —
  shared substrates the current per-table code keeps reinventing — never a migration of
  42 typed tables into one generic blob.
- Every empirical claim about the current system names its derivation (grep, file, todo
  section), per house convention.
- Where the ideal and the current system agree, that is a finding too, and it comes first.

---

## 0. The purpose, as the design input

One owner. Streams of evidence about their life (mail, messages, calendars, coursework,
documents). The system must: hold a truthful current model of the owner's situation;
derive obligations, plans and facts from evidence with provenance; notice when the world
moves (a deadline shifts, an enrolment decision moots a scholarship, a class stops
existing); repair its own beliefs; plan the day; and interrupt the owner only when
interruption pays. Everything auditable, everything reversible, nothing silently wrong.

That is the whole input. The design below was drawn from it, not from the schema.

---

## 1. What the ideal re-derives unchanged — the validation

Designing blind from the purpose, five of the current system's foundations fall out
almost immediately, which is the strongest possible evidence they are right:

| Re-derived from first principles | Current implementation |
|---|---|
| Observations must be immutable and kept — beliefs are re-derivable, evidence is not | `source_item`, docs/03 |
| Beliefs must be typed records, not text — reports are queries, not searches | commitment/engagement/fact/… |
| Every belief must cite its evidence | rule 1, `commitment_evidence`, quote verification |
| Expensive reading happens once, at ingest, tiered by cost | two-tier extraction |
| A failing input degrades the part, never the whole | rule 5 |
| Uncertain beliefs are quarantined, not asserted | rule 2, review queue, fact poison gate |

Nothing below proposes touching any of these. The divergences are all one level up — in
how beliefs *live* after they are created.

---

## 2. The greenfield pipeline

Six layers. Data flows down; change flows back up.

```
  SOURCES ──▶ OBSERVE ──▶ BELIEVE ──▶ STATE ──▶ ACT
                 │            │          │        │
                 └────────────┴──── CHANGE LEDGER ┘──▶ JUDGE (reacts to change)
```

### 2.1 OBSERVE — evidence with declared semantics

Every source declares, up front, which of three kinds it is:

- **Event stream** — each item happens once and is final (a mail, a message).
- **Snapshot** — each read asserts the complete current world (an ICS feed, a calendar
  window, a folder listing). Absence from a certified-complete read *means deletion*;
  content difference *means change*.
- **Append-only log with mutation** — items arrive once but carry fields that
  legitimately move upstream (an assignment's due date).

The observe layer stores two records per external thing: the **immutable observation**
(what was seen, when — forever) and, for snapshot/mutable sources only, a **mirror row**:
current upstream content hash, `first_seen`, `last_changed`, `gone_at`. The mirror is
mutable by design; the observation never is. Deltas — appeared, changed, disappeared —
are computed here, once, uniformly, and emitted as change events. A read that cannot be
certified complete emits nothing.

### 2.2 BELIEVE — claims on one lifecycle substrate

Typed tables stay (a commitment is not an engagement; the columns differ and should).
What is shared is the **lifecycle trait** every claim type carries identically:

- status machine (open → resolved/dropped/superseded), tombstones never deletes
- supersession chain with the reason on the link
- provenance (evidence ids + quotes, verified in code)
- confidence + the quarantine threshold
- a dedup identity rule declared per type
- **a recorded read-set**: which facts, mirrors and sibling claims this claim's current
  standing depends on

One module owns the transitions; each typed table declares its columns and its identity
rule. A new claim type costs a table and two declarations — not a new copy of staleness,
dedup, scrub and disposal.

### 2.3 CHANGE LEDGER — the keystone

One append-only table: every belief change and every mirror delta, as
`(when, kind, subject, field, old, new, cause)`. Nothing consumes state by re-scanning
tables; downstream work *subscribes to change*:

- a fact superseded → look up claims whose read-set names it → re-judge exactly those
- a mirror `due_at` moved → move the dependent claim, note the old value, notify
- a mirror `gone_at` set → retire the dependent claim, citing the certified read
- the open set changed → the plan's inputs changed → replan or knock
- anything changed → the state doc's next rendering differs → the diff is the story

Idempotency stops being a property each pass must prove separately: an unchanged world
emits no events, and no events means no work, structurally.

### 2.4 STATE — the evolving state doc as the hub

The state is a **fold over the claims, rendered** — who and where the owner is, the
active fronts, what is due, what recently changed (read off the change ledger, with both
values). Versioned; a new version only when the rendering differs; the diff between
versions is exactly the change-ledger window between them. It is never a second store —
every line carries the id of the claim behind it.

Every judgment pass and every model call reads the same state rendering. There is one
picture of the owner's situation, not one per caller.

### 2.5 JUDGE — one review engine, many rules

All belief-repair is one shape: `(subject, its evidence, the state) → verdict + citations`.
Deterministic rules and model rules are the same machinery with different executors.
Verdicts are keyed on `(subject, input-fingerprint)` — where the fingerprint covers the
read-set — so a verdict is *judged once per world*, and re-judged automatically when the
change ledger touches anything it read. Consequence asymmetry is policy on the engine,
not per pass: confident-drop needs citations verified in code; anything below threshold
becomes a question, never an action. Budget, bounding-per-run, and dry-run are engine
features, written once.

### 2.6 ACT — surfaces as pure readers; one interruption channel

Brief, dashboard, planner read state + claims. All owner interruption — questions,
notifications, the morning brief's "needs you" lines — flows through **one channel** with
one budget: ask-once identity, priority ordering, quiet hours, drip limits, and a single
inbox the owner drains. A question and a notification are the same object at different
urgencies.

Control plane: launchd fires a run every 30 minutes (closed decision, kept); the run is a
**reconciliation** — observe, diff, propagate, judge, act — not a fixed pass order. The
loop is identical from every entry point (CLI, app open, batch collect) because the loop
is the reconciler, not a command's epilogue.

---

## 3. Layer-by-layer: ideal / current / gap / path

### 3.1 Observe

- **Ideal:** declared source semantics; immutable observation + mutable mirror; uniform
  appear/change/disappear deltas; certified-complete reads.
- **Current:** `source_item` (immutable, universal — right). Semantics implicit. Snapshot
  handling exists only where a bug forced it: `source_item_retraction` + `retraction.py`
  for calendars (0030), `assignment` mirror for Canvas (0031). Mail/messages are event
  streams treated correctly by accident of being event streams.
- **Gap, with its incident trail:** the missing abstraction produced four separate
  incidents, each solved locally — calendar ghost timetable (no deletion concept,
  product-gaps §2), the canvas due-date cursor (a watermark on a describing field,
  lesson 2026-08-20), immutable-conflict spam (a legitimate upstream change re-announced
  every 30 minutes forever, audit §2), and the retraction near-miss (absence
  indistinguishable from failure, lesson 2026-08-20). Four costumes, one absent layer.
- **Path:** generalize goal 4's `assignment` pattern into a per-source **mirror**
  contract: any snapshot source gets (or shares) a mirror table with
  `current_hash / first_seen / last_changed / gone_at`, and `_ingest` computes deltas
  there instead of colliding with `content_hash` immutability. The immutable-conflict
  error class disappears as a side effect: a changed upstream item is a mirror delta,
  not a violation.

### 3.2 Believe

- **Ideal:** typed tables on one lifecycle substrate with recorded read-sets.
- **Current:** each claim type re-implements lifecycle alone. Derivation
  (`grep 'FROM …' over the repair modules`): `staleness.py` — commitment + open_question
  only; `scrub.py` — commitment only; `recheck.py` — commitment only; `dedup.py` —
  commitment only; `relevance.py` — judges commitments only (reads facts as input);
  `duplicates.py` — commitment + one engagement query. **Engagements, facts and
  assignments have no staleness, no scrub, no recheck, no relevance.** A stale
  engagement, a superseded-in-substance fact, a dead assignment row: invisible to every
  repair pass. And each new pass historically shipped commitment-first with the others
  as "a separate pass" that mostly never came (todo §7 "out of scope, noted not fixed").
- **Gap:** this is the deepest divergence. The system's repair intelligence is a
  property of one table, not of beliefs.
- **Path:** not a big-bang unification. Extract the lifecycle trait incrementally: one
  module (`lifecycle.py`) owning transition + supersession + tombstone + decision-row
  recording, adopted first by commitment (where all the code already is), then by
  engagement and fact as their repair coverage arrives. Read-sets arrive with the
  change ledger (3.3).

### 3.3 Change

- **Ideal:** every delta as a first-class event; downstream subscribes.
- **Current:** change is nowhere a first-class object. Four independent mechanisms exist
  because of that absence: `day_plan.inputs_fingerprint` (hash-the-world to detect drift,
  0028), `logic_check` judged-once (0029 — no way to know the world moved, todo goal 3:
  "the checker has no way to notice the situation moved"), goal 3's planned
  `commitment_dependency` (an invalidation index, facts→commitments only, unbuilt), and
  the conflict-spam class above. Each is a partial, local answer to "what changed since
  I last looked?"
- **Gap:** the keystone. One append-only `claim_event` table subsumes all four:
  fingerprints become "any event touching the plan's inputs since it was proposed";
  `logic_check` invalidation becomes "any event touching this verdict's read-set";
  goal 3's dependency table becomes the read-set generalized past facts; notify gains a
  real trigger stream instead of re-derived deciders.
- **Path:** this is where goal 3 should be built, slightly widened. Goal 3's A-increment
  already plans `commitment_dependency` + verdict supersession; implement it as
  `claim_event` + read-sets instead and the same work covers plans, notifications and
  the state doc's diffs. The todo's own design assumptions (tombstone-not-delete,
  rendering-not-store, `none` as first-class) all carry over intact.
  **This supersedes `tasks/pipeline-audit-2026-08-21.md` §3b's build order for the
  dependency half of goal 3** (there: build `commitment_dependency` as designed; here:
  build the generalized ledger instead). The state-doc half (A3/A6) is unchanged in both
  documents.

### 3.4 State

- **Ideal:** one versioned rendering, the hub every reader shares, diffs from the change
  ledger.
- **Current:** `context.assemble` — re-derived per call, capped small, three tiers,
  correct in spirit; `situation_doc` — designed (goal 3 A3), unbuilt, todo checkbox
  falsely `[x]` (audit §3a). No versioning, no diffs, no "what recently changed"
  anywhere in any prompt. And per audit §4a, the current rendering's situation section
  actually leads with the stalest overdue rows.
- **Gap:** the state exists as an ephemeral prompt block, not as the system's hub.
  Nothing can say *what changed this week* — which is precisely the owner's "evolving
  state doc" ask.
- **Path:** build A3 as designed (`situation.py`, versioned, hash-gated) but source the
  "what recently changed" section from `claim_event` rather than from a bespoke query —
  that is the fold-plus-diff of the ideal, and it makes the doc's evolution a free
  by-product instead of a feature.

### 3.5 Judge

- **Ideal:** one review engine; verdicts keyed on (subject, input-fingerprint);
  asymmetry, budget, bounds, dry-run as engine features.
- **Current:** five bespoke engines — triage, extraction, recheck, relevance, logic —
  each with its own idempotency key, its own cap wiring, its own bounding, its own
  citation verification. They agree in philosophy (the asymmetry rule is stated
  identically in recheck, relevance and logic docstrings) and share zero code. The
  cost is visible: CLI-initiated passes never persist `model_call` rows (todo §10),
  `logic.Report.errors` is dropped by its caller, each pass gets its own judged-once
  bug class.
- **Gap:** the philosophy is a convention, not a mechanism. Conventions drift — three
  files restating one rule is the tell.
- **Path:** a `review` table `(subject_kind, subject_id, pass, prompt_version,
  input_fingerprint, verdict, citations_json, decided_at)` with a partial unique on
  non-superseded rows, replacing `logic_check` and absorbing recheck's and relevance's
  judged-once keys. The runner grows out of `relevance.py` — it is already the most
  complete instance (bounded slice, cap-gated, citation-verified, dry-run).
- **Boundary kept:** deterministic rules (logic.py) and model rules stay separate
  executors — a regex disposal and a model verdict must never be confusable in the
  audit trail.

### 3.6 Act

- **Ideal:** one owner-interruption channel, one budget; identical reconcile loop from
  every entry point.
- **Current:** `open_question` (ask-once, floors, 5-a-day drip) and `notification`
  (dedup key, quiet hours) are two channels with two budgets and no shared priority;
  the scrub board exists because the question drip provably cannot drain a backlog
  (lesson 2026-08-20: "the arithmetic never converges"). The loop epilogue runs only
  from the CLI sync command, silently (audit §1a/1b).
- **Gap:** interruption is fragmented; the loop is an epilogue, not a reconciler.
- **Path:** short-term, audit §1's fixes (extract the chain, surface failures). Longer:
  merge question/notification delivery behind one prioritized channel — the tables can
  stay, the *budget and ordering* unify.

---

## 4. The divergence, in one sentence each, ranked

1. **Change is not a first-class object** — four mechanisms (fingerprint, judged-once,
   dependency table, conflict spam) are all local patches over the same absence.
2. **Belief lifecycle is a property of the commitment table, not of beliefs** —
   engagements, facts and assignments live outside every repair pass.
3. **Source semantics are implicit** — deletion and mutation were discovered per-source
   through four incidents instead of declared once.
4. **The state is a prompt block, not a hub** — no version, no diff, no shared picture.
5. **Judgment is five engines sharing a philosophy and no code.**
6. **Owner interruption is two channels with no common budget.**

## 5. The migration path, ordered, mapped onto the goals already open

Each step is additive, worktree-authored (the scheduler runs this checkout), sidecar
rebuilt in the same session as any migration:

1. **`claim_event` change ledger + read-sets** — build goal 3's increment A/B *as this*,
   widened past facts→commitments. Subsumes `inputs_fingerprint`, gives `logic_check`
   its invalidation, feeds notify and the state doc. The keystone; everything later
   subscribes to it.
2. **`situation.py` state doc** (goal 3 A3/A6 as designed) — fold + render, versions
   hash-gated, "recently changed" read from the ledger. Feed it to relevance (A5).
3. **Mirror contract for snapshot sources** — generalize `assignment`'s pattern;
   retire the immutable-conflict error class; calendar and files adopt it.
4. **`review` table + engine** — replace `logic_check`, absorb recheck/relevance keys;
   grown from `relevance.py`.
5. **`lifecycle.py` trait extraction** — commitment first, then engagement/fact/
   assignment gain staleness/scrub/relevance coverage by adoption, not by new modules.
6. **Unified interruption budget** — after 1–5, because priority needs the change
   ledger to rank on recency and the state doc to rank on relevance.

## 6. What not to do

- No generic claim mega-table; typed tables are load-bearing and closed.
- No in-process scheduler; the reconciler lives inside the launchd-fired run.
- No event-sourcing of the ledger itself; `claim_event` is derived history beside the
  tables, not a replacement for them — the ledger stays primary, the doc stays a
  rendering, retrieval stays additive.
- No rewrite of passes that work (triage, extraction, planner selection) to fit the
  abstraction; the abstraction is adopted where it removes duplicated machinery, and
  nowhere else.
