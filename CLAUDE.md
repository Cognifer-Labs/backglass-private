# CLAUDE.md

Read this first. It tells you what this project is, what order to build it in, and which
decisions are already made so you do not relitigate them.

## What this is

**Backglass** — a personal second brain. It reads the owner's email, documents, notes, and
calendar, and produces two surfaces:

1. **A morning brief**, pushed at 06:00 local, read in under two minutes.
2. **A dashboard**, opened during the day, showing schedule, commitments, and goals.

Single user. Runs entirely locally, for one owner who splits time across two timezones.

## The one idea that shapes everything

This is **not** a search tool over a document pile. Searchable email is strictly worse
than Gmail search, which already exists.

It is a **commitment ledger with documents as evidence.** Every incoming item is read
once, at ingest, by a model that extracts typed records: commitments, deadlines,
schedule blocks, goal checkpoints. The brief and dashboard are reports over those
records.

Consequence: **do not build a vector store, embeddings, or RAG.** The queries are known
in advance. If you find yourself reaching for semantic search, you have misread the
architecture. See `docs/02-architecture.md`.

## Decisions already made — do not re-open

| Decision | Where |
|---|---|
| SQLite, not Postgres, until there is a second user | `docs/02-architecture.md` |
| Two-tier extraction: cheap triage, then careful extract | `docs/02-architecture.md` |
| Raw source items are immutable and kept forever | `docs/03-data-model.md` |
| `user_id` on every table even though it is always 1 | `docs/03-data-model.md` |
| Credentials live in a table, not env vars | `docs/07-connectors.md` |
| Cream `#FCF8EC` and black, five reserved inks | `design/design-system.md` |
| No shadows, no rounded corners, no gradients | `design/design-system.md` |
| Three chart series maximum | `design/design-system.md` |
| Python + uv, SQLite + raw SQL, FastAPI + Jinja2 + HTMX | `docs/10-tech-stack.md` |
| launchd for scheduling, never an in-process scheduler | `docs/10-tech-stack.md` |
| Transactional email provider, never the Gmail API | `docs/10-tech-stack.md` |
| Tests and evals are separate; evals never gate CI | `docs/10-tech-stack.md` |

## Build order

Do these in sequence. Do not start a phase until the previous one runs clean twice.

0. **Validate without building** — `docs/09-build-plan.md` §Phase 0. No code.
1. **Ledger + Gmail extraction** — schema, ingest, triage, extract. Success is a SQL
   query returning real open commitments.
2. **Morning brief** — generation and delivery. Run it a week before adding anything.
3. **Dashboard** — read-only, then write-back.
4. **Schedule + goals** — the day planner and goal engine. `docs/04-daily-schedule-and-goals.md`
5. **Widen intake** — Drive, notes, calendar, Canvas.

## Reading order for you

1. `docs/01-product-brief.md` — the why, condensed
2. `docs/02-architecture.md` — the five stages
3. `docs/03-data-model.md` + `specs/schema.sql` — the tables
4. `docs/10-tech-stack.md` — what to build it with, and what was rejected
5. `docs/11-ux-flows.md` — the nine flows end to end
6. Then whichever phase doc matches the work in front of you

`PROMPT.md` has a session-by-session kickoff prompt for each phase. If the owner pasted
one of those, follow it and this file together; the prompt scopes the session, this file
sets the rules.

## Rules that are load-bearing

These are not style preferences. Violating any of them breaks the product.

1. **Every generated claim links to its source.** A brief line with no provenance does
   not ship. Trust collapses after two unsourced wrong claims and never comes back.
2. **Low-confidence extractions go to a review queue**, never into the brief as fact.
3. **Idempotent by default.** Two consecutive runs with no upstream changes produce
   zero writes. If you cannot assert this in a test, the sync is wrong.
4. **Relative dates resolve against the source item's timestamp**, not the run time.
   "By Friday" in a three-week-old email is not this Friday.
5. **A failing source degrades, never blocks.** Log it, surface it in the Sources panel,
   continue, exit non-zero at the end.
6. **Respect the client-data boundary** in `docs/08-privacy-and-data-boundary.md`. This
   one has legal weight, not just hygiene.
7. **Spend cap is enforced in code.** When hit, degrade to triage-only rather than
   silently overspending.

## Stack

Python 3.11+. SQLite. No web framework for the pipeline; the dashboard is a small
FastAPI app serving one page. No frontend framework — the design system is plain CSS
custom properties and the markup is server-rendered HTML.

Reason: the whole thing should be readable in an afternoon by one person a year from now.

## Testing expectations

- Every extraction prompt has a fixture set of real-shaped inputs with expected output.
- A test that carves a fragment out of rendered HTML uses `tests/conftest.py::panel_slice`
  (bounded by stable `id="panel-…"` markers) — never `.split()` on a closing tag or
  element order. Closing tags lie once elements nest, and N inline splits means N breaks
  on the next markup change. Before restructuring shared markup, grep tests for
  structural couplings and fix them in the same change.
- Idempotency test: run the sync twice against a frozen fixture, assert zero writes on
  the second pass.
- Date-resolution tests specifically for relative dates across timezone changes, because
  the owner moves between UTC-7 and UTC+5:30.
- No test may call a live API. Record fixtures instead.

## Owner memory (Phase 12)

Durable facts about the owner live in the `fact` table — the personal knowledge base.
Before hunting through mail, files, or old sessions for who/what/where facts, run
`uv run backglass memory export` (or read the Memory page). When you learn a new
durable fact about the owner, write it back: `uv run backglass memory set <subject>
<key> "<value>" --note "<evidence>"`. Supersession keeps history; never edit rows.
