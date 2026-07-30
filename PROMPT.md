# Claude Code prompts

Copy-paste, one per session. Do not run them all in one session; each phase should end
with something that works, and a fresh session keeps context focused.

---

## Session 0 — orientation and plan

Run this first, before any code. It produces a plan you approve, not files.

```
Read CLAUDE.md, then docs/01 through docs/11 in order, then specs/schema.sql.

Do not write any code yet.

Then give me:
1. A one-paragraph statement of what this system is, in your own words. If your
   description mentions search, retrieval, or embeddings, you have misread it — say so
   and re-read docs/02.
2. The five things most likely to go wrong in this build, ranked, with the doc that
   addresses each.
3. Your implementation plan for Phase 1 only (docs/09), as a task list with file paths.
4. Any place where two docs contradict each other, or where a spec is too vague to
   implement. I would rather fix the spec now than discover the gap in week three.

Ask me about anything ambiguous before you propose the plan.
```

---

## Session 1 — scaffold and schema

```
Implement Phase 1 scaffolding from docs/09 and docs/10.

Scope for this session, nothing beyond it:
- uv project, pyproject.toml, ruff + mypy + pytest config
- the package layout in docs/10 §Layout, with empty modules and real docstrings
- db/ connection helper: WAL, foreign_keys, dict row factory
- migrations/0001_initial.sql from specs/schema.sql, plus a schema_version table and a
  migration runner that refuses to start on a version mismatch
- typer CLI with every command in docs/10 §CLI stubbed and --help complete
- `backglass init` fully working

Then: run init against a temp db, show me the resulting schema, and write the first
test — that init is idempotent.

Do not implement any connector, extractor, or route this session.
```

---

## Session 2 — data boundary and Gmail ingest

The boundary comes before the connector on purpose. Read docs/08 twice.

```
Implement connectors/boundary.py and connectors/gmail.py per docs/07 and docs/08.

Order matters:
1. boundary.py first, with its tests, before any network code exists. The test from
   docs/08 D7 — a message with a denylisted address in ANY recipient field produces zero
   rows — must pass before you write the connector.
2. Then the Gmail connector: incremental via historyId, quoted history stripped before
   hashing, cursor in the credential table.
3. The boundary check is called by the connector before it yields, per docs/10 §Layout.
   Not a filter, not middleware.

Record vcrpy fixtures for the Gmail calls. No test may hit a live API.

When done, show me: the boundary test output, and `backglass sync --dry-run` against the
fixtures printing what it would write.
```

---

## Session 3 — extraction

```
Implement extract/ per docs/02 §Two-tier extraction and the prompts in
specs/extraction-prompts/.

Requirements I care about most:
- Pydantic v2 schemas are the single source of shape; generate the Anthropic tool JSON
  Schema from them so they cannot drift (docs/10 §Model layer).
- Force structured output via tool use, not by asking for JSON in the prompt.
- Prompts load from the .md files at runtime; the version from frontmatter is stamped on
  every row. Never inline a prompt in Python.
- Relative dates resolve against source_item.occurred_at, never against now. Write the
  test for this first, including a fixture that is three weeks old, and including a
  Phoenix-to-Kolkata timezone move.
- Post-processing steps 1 through 5 in extract-commitments.md are code, not prompt.
- Spend cap enforced in code: at the cap, degrade to triage-only and set run.degraded.

Build the fixture set listed at the bottom of extract-commitments.md. All seven cases.

Then show me the idempotency test passing: sync twice over frozen fixtures, second run
writes zero rows.
```

---

## Session 4 — the brief

```
Implement brief/ per docs/05.

Non-negotiable:
- Under 400 words, enforced in code. If it would exceed, drop the lowest-priority section
  and say that it was dropped.
- Every line carries a source link. A line without provenance does not render — make that
  a hard failure in the renderer, not a warning.
- Empty sections are omitted entirely, not rendered empty.
- No item appears in two sections; precedence is the section order in docs/05.
- Inline styles only, table layout, background set explicitly on the outer table.
  Colors from design/tokens.css, per design/design-system.md §9.
- Generation failure sends a short failure notice, never silence.

Render three fixtures to HTML files I can open: a normal day, a day where Gmail auth has
expired, and a day that is fully booked with no deep work slot available.
```

---

## Session 5 — dashboard

```
Implement web/ per docs/06 and docs/11 §3, §4, §8.

Stack is FastAPI + Jinja2 + HTMX, no build step, no npm (docs/10 §Web layer).

Read-only first, all seven panels, matching design/preview.html — open it and match it,
including the black section bars, the 2px keylines, and the reel digits.

Then write-back on all seven actions from docs/06. Each returns the re-rendered fragment,
HTMX swaps it in place. No page reloads, no toasts.

Two things I will check specifically:
- The review queue: Accept and Reject are visually equal weight, neither preselected
  (docs/11 §4).
- The Sources panel shows triage kill rate, and a failed source turns its row vermilion
  and stays that way.

Run scripts/validate-palette.mjs before you finish. It must exit 0.
```

---

## Session 6 — schedule and goals

The biggest phase. Split it if it gets long.

```
Implement plan/ and goals/ per docs/04. Follow the order in docs/09 Phase 4 exactly:
capacity first, then planner, then rollover, then goals, then staleness and risk, then
checklist, then the weekly rituals.

The requirements I will test against:
- P1/P2: never select past capacity; report overflow explicitly, never truncate silently.
- P3: capacity under 60 min produces no plan, just what is due.
- P6-P9: one protected 90-min block in the peak window when capacity allows, and the
  "no deep work available today" line when it does not.
- P10-P12: rollover to the top of tomorrow, and the drop-or-do question on the third.
- P13-P16: timezone from explicit config with a date range, never geolocation. Phoenix to
  Coimbatore must not schedule a 03:00 block.
- G5-G7: Monday capacity check names the gap in hours. It never drops a target.
- G11-G13: staleness and risk computed independently, never merged into one score.
- C1: seven-item checklist cap enforced in application code with an error that explains
  why. C4: broken streak resets silently, no copy.

Then run every acceptance criterion in docs/04 §5 as a test and show me the results.
```

---

## Session 7 — widen intake

```
Implement the remaining connectors per docs/07: Drive, Obsidian, Calendar, and optionally
Canvas.

Each one is the same Protocol against a pipeline that already works. If any of them needs
a change to the ledger schema, stop and tell me — that means the schema is not
source-agnostic and I want to fix that rather than special-case it.

Obsidian is a vault directory watch, no API, no auth. Do that one first; it is the
cheapest and it will surface any assumptions the Gmail connector baked in.
```

---

## Useful mid-session prompts

```
Before you continue: which rule in CLAUDE.md does this change come closest to violating?
```

```
Show me the diff and explain the three decisions in it I am most likely to disagree with.
```

```
This is nondeterministic. Is what you just wrote a test or an eval? If it calls a model,
it is an eval and it does not belong in CI (docs/10 §Testing).
```

```
Re-read docs/08 and tell me whether anything in this session's changes could allow a
denylisted message to reach the extractor.
```
