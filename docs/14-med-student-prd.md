# PRD — Med-student planning and organization layer

Status: **draft for owner review**. Written 2026-07-30. Nothing here is approved for
build; every feature below needs the usual plan → approval → phase cycle.

## 1. Why

The owner is applying to medical school while running a company. Backglass already
holds the general machinery — commitment ledger, morning brief, day planner, goal
engine, roadmaps, eleven connectors including Canvas — and Phase 10 added the first
med-specific piece: the medical roadmap v2 with AMCAS-category hour totals.

What it does not yet hold is the *application itself* as a first-class object: the
school list, the secondary-essay queue, the letter writers, the MCAT score trajectory,
the spaced-repetition workload (Anki/Avorio), and the AMCAS Work & Activities section
that all those logged hours eventually have to become. Today those live in
spreadsheets, MSAR tabs, and memory — exactly the "commitments made in prose, tracked
nowhere" problem docs/01 was written to kill.

This PRD maps the full set of planning/organization needs of a premed through the
application cycle, marks what Backglass already covers, and specifies the gaps as
buildable features that respect every standing architectural decision.

## 2. What already exists (do not rebuild)

| Med-student need | Already covered by |
|---|---|
| Hour accumulation toward AMCAS categories | `total` targets, medical preset v2 (0006, Phase 10): shadowing 60 / clinical 150 / non-clinical 100 / research 200 / leadership 50, log form with org+supervisor note |
| Long-arc milestones (MCAT date, primaries, Step 1) | Roadmap steps → milestone targets, staleness + risk sentences (G11–G14) |
| Weekly practice cadence (FL sections, secondary drafts) | Cadence targets + weekly progress tracks, consistency heatmap |
| Assignment deadlines | Canvas connector → commitment extraction |
| Study-block scheduling around classes/meetings | Day planner, capacity model, protected block, timezone handling |
| Daily habits (reviews, question banks) | Checklist (7-item cap), streaks, week tick-grid |
| Nudging people who owe you something | Follow-up nudges (Phase 6 S8), People pages |
| One trustworthy morning surface | Brief with hard provenance rule (B2) |

The pattern for every new feature below is therefore **extend the existing engines**,
not build parallel ones. If a proposal here cannot be expressed as connectors →
source_items → typed records → SQL read paths, it is misdesigned.

## 3. Gaps and proposed features

Ordered by priority. P0 = the application cycle fails without it this year.
P1 = materially better outcomes. P2 = quality of life.

---

### F1 — Extracurricular activity registry + AMCAS Work & Activities export  (P0)

**Problem.** Phase 10 logs hours against category totals, with org/supervisor as free
text in a checkpoint note. AMCAS wants more: up to **15 discrete activities**, each
with organization, role/title, contact (name + phone/email), start/end dates,
total hours, and a **700-character** description — plus **3 "most meaningful"**
selections that get an additional **1325-character** essay each. Free-text notes
cannot be reassembled into that reliably, and the contact info for a supervisor from
sophomore year will be unfindable in two years.

**Proposal.**
- Migration: new `activity` table — `user_id, title, org, role, category`
  (maps to the total-target key: shadowing/clinical/volunteering/research/leadership),
  `contact_entity_id REFERENCES entity(id)` (supervisors become People, so the
  existing profile/touch/nudge machinery applies to them for free),
  `started_on, ended_on, is_ongoing, most_meaningful INTEGER DEFAULT 0`.
- `checkpoint` gains nullable `activity_id` (additive migration; existing rows
  untouched). The roadmap-page log form grows an activity picker; hours logged
  without an activity keep working exactly as today.
- Per-activity hour totals are `SUM(delta)` on read, same G3/G10 rule as targets —
  no cached counters.
- **Export**: `backglass amcas-export` + a read-only report page. Emits each
  activity with computed hours, date range, contact block, and the concatenated
  checkpoint-note stream as raw material for the 700-char description (character
  count shown against the 700/1325 limits; the owner writes the prose — the tool
  never generates application text, it assembles evidence). Every hour figure in
  the export links back to its checkpoints (provenance rule holds even here).
- Cap surfaced, not enforced: a quiet count "12 of 15 activity slots used" —
  AMCAS enforces its own cap.

**Not doing:** auto-writing descriptions with a model (application text is the
owner's voice, and a hallucinated hour count in a submitted AMCAS is the exact
trust collapse rule 1 exists to prevent); TMDSAS/AACOMAS export formats (record
the field mapping in a doc, build if the owner actually applies through them).

---

### F2 — Spaced-repetition connector: Anki + Avorio  (P0)

**Problem.** Daily card reviews are the single biggest recurring time cost of premed
and preclinical study, and Backglass is blind to them: the planner cannot reserve
capacity for a 90-minute review backlog it cannot see, the checklist tick for
"reviews done" is manual and unverified, and streak/consistency data lives in the
review app instead of the heatmap.

**Proposal.** One connector per app, both following the established local-read
patterns (no new deps, no APIs where a file exists):

- `connectors/anki.py` — read Anki's local SQLite (`collection.anki2`) the way
  `imessage.py` reads `chat.db`: `mode=ro&immutable=1`, never while holding a write
  lock anyone cares about. Cursor = max `revlog.id` (it is an epoch-ms primary key,
  monotonic — same ROWID-watermark shape as iMessage). Each sync emits one
  source_item per day per deck-group: reviews done, minutes spent, and cards due
  tomorrow (from the scheduler tables). AnkiConnect is rejected: it requires the
  Anki GUI running; a file read does not.
- `connectors/avorio.py` — same shape against Avorio's store. Avorio
  (`/Users/Dharsan/Avorio`) is the owner's own flashcard app — Rust core over a
  local, offline-first SQLite database, FSRS-5 scheduling, shipped on macOS/iOS —
  so the connector reads its review log the same immutable-read way, and because
  it is first-party, any awkwardness in the schema is fixed by adding a stable
  export view *in Avorio*, not heroics in the connector. FSRS retrievability
  projections (already computed in Avorio's stats) make the cards-due-tomorrow
  figure exact rather than estimated. Exact table names and the macOS db path
  need one confirmation pass in the Avorio repo before the migration is written
  — see §7 Open questions.

**What the records feed (all existing engines):**
- A `reviews` **cadence target** on the study goal: review-day checkpoints arrive
  with `source='extraction'` + the source_item, so the heatmap and weekly track
  fill themselves. The manual checklist item for reviews is retired.
- **Capacity**: cards-due-tomorrow × measured seconds-per-card becomes a planner
  line item, same way calendar events already reduce capacity. The estimate uses
  the owner's own trailing average from revlog, not a guess.
- **Brief**: one line — "Reviews: 140 due (~45 min), 12-day streak" — with
  provenance to the source_item. Only appears when a backlog or streak-break risk
  exists (B3: empty sections are omitted; a normal day says nothing).

**Idempotency:** re-sync with no new reviews writes zero rows (content_hash on the
per-day summary), and the cursor test asserts the second run *fetches* nothing —
the 2026-07-30 lesson about watermarks applies verbatim.

---

### F3 — Application cycle tracker: schools, secondaries, interviews, decisions  (P0 once the cycle starts)

**Problem.** An application cycle is 20–40 parallel state machines: each school has
its own secondary prompts, its own deadline, an interview date, and a decision.
Rolling admissions makes latency the enemy — the medical.md preset already encodes
the two-week secondary SLA as a cadence, but nothing tracks *which school* is
waiting, and a missed secondary is invisible until it is fatal.

**Proposal.**
- Migration: `school` (`name, tier_note, added_on, withdrawn_on`) and
  `school_event` (`school_id, kind, occurred_at, due_on, source_item_id, note`) —
  kind enum: `primary_submitted, secondary_received, secondary_submitted,
  interview_invite, interview_scheduled, interview_done, accepted, waitlisted,
  rejected, withdrawn`. Append-only events, state derived on read — same
  philosophy as checkpoints, so status can never go stale and there is nothing to
  un-corrupt.
- **The connectors do the data entry.** Secondary invitations, interview invites,
  and decisions arrive as email. Extraction gains a `school_event` record type
  (two-tier as always: triage already sees these; a new versioned prompt with a
  fixture set extracts kind + school + deadline). Low-confidence school matches go
  to the review queue, never straight into the tracker — rule 2.
  `secondary_received` auto-creates a commitment: "Return {school} secondary",
  due = received + 14 days, provenance = the email. The existing board, brief, and
  resolve flow handle the rest with zero new UI.
- **Surface:** a Schools section on the Roadmaps page (it is the application
  roadmap's detail, not a sixth nav tab): one row per school, derived status chip,
  days-waiting where the ball is in the owner's court, vermilion keyline on any
  secondary older than 10 days. Read-only page + quick-add form; the write path is
  extraction plus manual event entry.
- **Brief, in season:** "3 secondaries in flight — oldest 11 days (UChicago)" with
  source links. Bounded at the existing ≤3-line section budget.

**Not doing:** MSAR data import or school-list building advice (Backglass tracks
the owner's decisions, it does not make them); scraping portals (email already
carries every event; portals have no stable API and rule "no live API in tests"
would be the least of the problems).

---

### F4 — MCAT score trajectory: the `metric` target kind  (P1, becomes P0 in the MCAT-prep block)

**Problem.** The goal engine counts things (cadence, total) and dates things
(milestone). An MCAT full-length score is neither — it is a **measurement moving
toward a threshold** (e.g., FL1 505 → FL4 512, goal 515). Today there is nowhere to
put it, so the single most decision-relevant number of the prep block (sit, delay,
or void?) lives outside the system.

**Proposal.**
- Migration 000x: `target.kind = 'metric'` + `target.metric_goal INTEGER`;
  `checkpoint.delta` already carries an integer — for metric targets it holds the
  measured value instead of an increment (`checkpoint.note` carries which FL and
  section splits). Progress on read = latest value, trend = last three.
- `goals/health.py` gets a metric-risk sentence parallel to G13: "FL trend +2/test;
  on pace for 511 by test date, goal 515" — same observed-rate machinery, different
  aggregation (last-value, not sum). Staleness applies unchanged (an FL target
  quiet for three weeks in a prep block is exactly what staleness exists to say).
- Score entry is the existing log form; an FL is also a 7.5-hour fixed event, which
  the planner already handles once it is on the calendar.
- Chart discipline: the trajectory renders as one series (score over time) with a
  goal rule — well inside the three-series maximum.
- Generic by construction: the same kind later holds Step-1 practice scores, a
  GPA, or a body-weight goal. Nothing in the engine says "MCAT".

---

### F5 — Letters of recommendation tracker  (P1)

**Problem.** Letters are commitments *other people* have made to the owner — the
literal `owed_to_me` case the ledger was built for — with a twist: they need
staged nudging (ask → agreed → drafted → submitted) across months, and nagging a
letter writer badly costs more than the letter.

**Proposal.** Minimal new schema; mostly composition of existing pieces:
- Letter writers are People (entities with profiles) — already true of anyone
  emailed. Add a `letter` table: `entity_id, status
  (planned|asked|agreed|submitted|declined), asked_on, needed_by, source_item_id`.
- Status changes ride the existing extraction path where possible ("I've submitted
  your letter" is a resolving message; supersession already handles it) with
  manual fallback on the person page.
- Follow-up nudges (Phase 6 S8) gain letter awareness: an `agreed` letter with no
  submission 3 weeks before `needed_by` surfaces in the brief's nudge section —
  within its existing ≤3-line, curated-only budget. Tone is the owner's problem;
  Backglass only surfaces timing.

---

### F6 — Coursework: prereq coverage + GPA lens  (P1)

**Problem.** Canvas already yields assignment commitments, but two derived numbers
drive the whole application — cumulative GPA and BCPM (science) GPA — and the
prereq step in medical.md is a single unstructured milestone with no notion of
which requirements remain.

**Proposal.**
- `course` table: `title, term, credits, grade, is_bcpm, prereq_key
  (bio|chem|orgo|physics|biochem|math|english|null)`, entered manually per term
  (grades are a handful of rows a semester; a registrar connector is not worth
  its maintenance). Canvas course IDs link where they exist.
- Read paths: cumulative + BCPM GPA (AMCAS 4.0 scale, computed in SQL, shown with
  the standard caveat that AMCAS recalculates); prereq checklist derived from
  `prereq_key` coverage — the medical roadmap's prereq step gets a real progress
  track instead of a binary milestone.
- Surfaces: a quiet block on the Goals page during term; GPA never appears in the
  brief (it changes once a term; the brief is for today).

---

### F7 — Cycle-aware brief seasons  (P2)

**Problem.** The brief's sections are date-agnostic, but a premed year has sharp
seasons where the highest-value line changes: May–June (primary submission — days
matter under rolling admissions), July–September (secondary SLA), September–February
(interview logistics + prep), March–April (decisions/waitlist letters of intent).

**Proposal.** No new data — a season function over the application roadmap's step
dates reweights existing sections: in the secondary season the F3 line leads; in
interview season, tomorrow's interview and its prep block lead. Season boundaries
come from the owner's actual roadmap dates, never a hardcoded calendar (AAMC dates
shift yearly; the roadmap is already the owner's copy of them). Strictly a
`brief/daily.py` ordering concern; B1–B4 budgets unchanged.

---

### F8 — Interview prep pack  (P2)

One-page-per-school prep view assembled entirely from existing records: every
commitment/interaction with people at that school, the secondary essays sent
(Drive evidence links), the school_event history, and travel blocks on the
calendar. Zero new tables — it is a read-path join, which is the architecture
working as intended. Build only when interviews actually land.

## 4. Sequencing

Respecting the build-order rule (a phase starts only after the previous runs clean):

1. **Phase A (now):** F1 activity registry + F2 Anki/Avorio connectors. Both are
   pure extensions of shipped engines (Phase 10 totals; connector protocol), both
   compound with time — every week without them loses evidence (unattributed
   hours, unmeasured reviews).
2. **Phase B (pre-MCAT block):** F4 metric targets. Small migration, high decision
   value, needed before the first FL is sat.
3. **Phase C (before the cycle opens, ~May):** F3 schools + F5 letters, together —
   they share the extraction-prompt work and the in-season brief budget.
4. **Phase D (during term / in season):** F6 GPA lens, then F7 seasons, then F8
   prep packs as interviews arrive.

Each phase = the usual: migration + engine + read paths + fixtures + idempotency
tests + both-theme screenshots; evals for any new extraction prompt.

## 5. Non-goals

- **No model-written application content.** Essays, descriptions, and "most
  meaningful" text are assembled-evidence surfaces, never generated prose.
- **No advice engine.** School-list strategy, "chance me", target scores — out.
  The system reports state; the owner decides.
- **No portal scraping**, no MSAR import, no AAMC API (none exists for applicants).
- **No new nav tabs.** Everything lands on the five existing pages; F3 lives under
  Roadmaps.
- **No second product.** Avorio remains the review app; Backglass reads its
  exhaust. Study-content features (deck building, card stats UI) stay in Avorio.
- Standing decisions untouched: SQLite, raw SQL, no embeddings/RAG, launchd,
  spend cap, provenance on every generated claim.

## 6. Success criteria

- The AMCAS Work & Activities draft is assembled from the export in under an hour,
  with zero hour-counts the owner cannot source-link. (F1)
- The planner's capacity line reflects real review load within ±15 minutes on a
  normal day, with no manual entry. (F2)
- During the cycle: no secondary exceeds 14 days unflagged; the brief names the
  oldest one every morning it exists. (F3)
- Sit/delay decision before the MCAT is made from the metric trend line, not a
  spreadsheet. (F4)
- Six weeks after Phase A: the brief still gets opened most mornings, and at least
  one med-track item per week surfaces that would otherwise have been missed —
  the docs/01 bar, unchanged.

## 7. Open questions (need owner rulings)

1. **Avorio integration surface.** Read the local SQLite store directly (fast to
   build, couples Backglass to Avorio's internal schema, which is still moving —
   Android port in flight), or add a versioned export view/table inside Avorio
   (one small first-party change, stable contract, also useful to Avorio's own
   stats)? The export view is the recommendation; needs the owner's call since it
   touches the Avorio codebase. Also: which device's db is canonical for review
   history when macOS and iOS both hold one — does Avorio's optional Supabase
   sync make the Mac db complete, or is it per-device?
2. **Activity ↔ total-target linkage.** Is `activity.category` keyed to the five
   preset total keys (simple, breaks if a total is renamed) or FK'd to `target.id`
   (robust, couples activities to one roadmap instantiation)?
3. **School matching confidence.** Extraction maps "UChicago Pritzker" ↔ the
   school row. Alias table like entities, or review-queue every ambiguous match in
   season? (Season volume is high; queue fatigue is real.)
4. **Does F3 warrant its own preset?** An `application-cycle` roadmap distinct
   from the four-year medical.md, instantiated the May the cycle opens — or fold
   school events under the existing medical roadmap's `primaries`/`secondaries`
   steps?
5. **Course grades in the ledger at all?** F6 is the least ledger-shaped feature
   here (manual entry, term granularity). Cut it if it doesn't earn its rows —
   a spreadsheet may honestly win.
