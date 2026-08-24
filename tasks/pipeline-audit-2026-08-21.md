# Pipeline audit — loop, logic, state doc, knowledge map (2026-08-21)

Read-only session. Every claim names its derivation, per the `backglass state` convention.
Companion to `tasks/audit-2026-08-21.md` (another session's backend audit, cited not
restated) and `tasks/product-gaps-2026-08-20.md`. Fresh numbers here are from
`uv run backglass state --json` read today at HEAD 24773d3 with the dirty tree.

## The purpose, restated as the audit's yardstick

Backglass is a commitment ledger with documents as evidence — not search over a pile.
Every incoming item is read once at ingest by a two-tier extractor; the brief, dashboard
and planner are reports over the typed records. The ledger stays primary; retrieval is
additive, never load-bearing. Since 2026-08-18 the goal on top of that is the full-picture
loop: three-tier memory into every model call, a checker that disposes of what the record
contradicts, a situation doc, dependencies that invalidate stale verdicts, and a plan that
re-plans on drift. It is the primary record of the owner's ASU college journey
(memory: college-journey-primary-use), so reliability of this loop outranks everything
cosmetic.

Measured against that: the ingest→triage→extract spine is healthy (0 untriaged, 0
unextracted, retrieval fully indexed 1522/1522, schema 31/31). The gaps are all in the
*recognition loop* wrapped around it.

---

## 1. The loop

### 1a. The brain of the loop runs only from the CLI — HIGH

The post-sync chain — catchup → replan → logic → questions.refresh → notify — lives in
`__main__.py`'s `sync` command (`__main__.py:427–495`), not in `sync.sync()` itself.
Derivation: `grep catchup\|logic_mod\|notify_mod\|replan backglass/web/app.py
backglass/batch.py` — app.py runs catchup only (`spawn_on_open`), batch.py runs none.

Consequences:
- A batch collect that extracts hundreds of items triggers no replan, no logic pass, no
  questions, no notifications.
- The desktop app opening fills a missing plan but never re-plans a drifted one, never
  disposes, never notifies.
- Any future caller of `sync()` silently gets a pipeline without the recognition loop.

Suggestion: extract the chain into one function (`backglass/postsync.py` or a
`_post_passes(conn, settings, report)` in sync.py) that every entry point calls, reporting
through `SyncReport` rather than `typer.echo`. The passes are already best-effort
internally; centralizing them is a move, not a redesign.

### 1b. Four post-passes fail silently — HIGH

`replan`, `logic`, `questions.refresh`, `notify` each sit in
`except Exception: pass` (`__main__.py:445–495`). No `report.errors` append, no echo, no
exit-code effect. Rule 5 is degrade *and surface, exit non-zero*; these degrade
invisibly. A detector that starts throwing — after a migration, a schema change, a bad
fact — dies forever with no signal anywhere. Worse, `logic.Report.errors` (per-rule
failures collected inside `check`) is discarded by the caller: only `disposed.applied` is
read.

Suggestion: append `f"replan: {exc}"` etc. to `report.errors` (or a post-pass error list
printed and counted in the exit code). One line per block.

### 1c. Pass ordering: replan before logic — MEDIUM

The chain runs replan *then* logic *then* questions. A logic disposal changes the open
set (canvas-past-grace will drop 44 rows by mid-September per product-gaps), which
changes the planner pool and hence `inputs_fingerprint` — but replan already ran, so the
drift is caught one sync (30 min) later, or at 05:45. Suggestion: logic → questions →
catchup → replan → notify. Dispose first, ask second, plan around the cleaned board,
knock last.

### 1d. The dated jobs are still firing on IST — outstanding owner action, live today

`state --json` schedule.drifting: plan last ran **17:18**, brief **17:30**, all jobs
offset uniformly (05:45 → 17:18 ≈ IST−MST). The 2026-08-17 lesson and product-gaps §1
diagnosed this (`UserEventAgent-Aqua` stale zone, SIP-protected, **only a reboot
clears it**) and the reboot has not happened: the morning plan is still built at 5pm for
a day that is over, and the morning brief is still an evening brief, today. Nothing in
code fixes this. It is the single highest-leverage action on the list and it is a
restart.

Verify per the lesson: arm a throwaway launchd job two minutes out and watch its log —
`launchctl print` agreeing proves nothing.

### 1e. Already-documented loop defects, cited

From `tasks/audit-2026-08-21.md`, still open and still true at today's read:
- **Immutable-conflict spam saturates the failure signal** (§2): the five moved CIS 236
  assignments re-emit `content changed for an immutable source_item` every 30 minutes
  forever; sync exits 1 every run, so a real failure is invisible. Fix shape: report a
  known `(external_id, content_hash)` disagreement once, not per run.
- **Doctor reads last-run status, not a rate** (§3): reminders failed 28/31 runs on
  08-20 and read `ok`.
- **The 30-minute loop is "while awake"** (§6): 31 real runs/day against 48 nominal, one
  33-hour gap. Freshness reasoning should read the last run, never assume cadence.
- **Spend cap cannot fire on this backend** (§1 + addendum): imputed spend, cap raised to
  45000c until 2026-09-01 — reset it when the month rolls.

### 1f. The sidecar is behind again — 7 stale surfaces

`state`: `matches_source: false` (dedup.py, entities.py, search.py, app.py, panels.py,
_board.html, _review.html). The scheduler runs this checkout (2026-08-21 lesson), so the
tree is live but the installed app is not. The classes-page todo's last item ("rebuild
the desktop sidecar, or say plainly that it is behind") is still open. It is behind.

---

## 2. The logic checker (`backglass/logic.py`)

The design is sound and the boundary is right: positive contradiction only, provenanced
disposals, `moot` revivable while `dismissed` stays permanent, one owner-ruled exception
(canvas grace) explicitly fenced. Suggestions are about reach and lifecycle, not the
boundary.

### 2a. Verdicts are judged once, ever — the todo's own headline gap, still open

`logic_check` is `UNIQUE (user_id, commitment_id)` (migration 0029). A `keep` issued when
the facts said one thing is never revisited when a fact supersedes. Goal 3's
`commitment_dependency` invalidation index is the designed fix and none of it exists yet
(see §3). Until it lands, the checker structurally cannot notice the situation moved —
which is the owner's 2026-08-20 complaint restated in schema.

### 2b. Rule errors are collected and then dropped

`check()` catches per-rule exceptions into `Report.errors` — correct unit — but the sync
wrapper reads only `applied` and the outer `except: pass` eats the rest (§1b). A rule
that breaks disappears rather than degrading loudly.

### 2c. `_canvas_assignments_past_grace` should eventually key on the `assignment` table

The blanket 7-day grace was right when nothing tracked upstream state. Migration 0031's
`assignment` rows now carry `due_at` and `last_changed_at` per feed read, and goal 4
increment B2 already assigns "gone from the feed" to `retraction`'s shape. When B lands,
the grace rule can consult the typed record (still in the feed? due date moved?) instead
of elapsed time alone — shrinking the module's one absence-of-evidence exception. The
module's own note (delete if `CANVAS_TOKEN` is granted) stands.

### 2d. Minor

- `_questions_the_calendar_no_longer_supports` reaches into `questions_mod._conflicts` /
  `_untitled` (private seam). Fine at this scale; worth a public
  `questions.detect(kinds=…)` the day a third caller appears.
- `_reported_done` decides off the commitment's own title regex — the weakest evidence in
  the file, but it resolves-with-note rather than drops, and the near-miss ("zip it up
  once done") is a test. Acceptable as is.

---

## 3. The state doc (situation doc)

### 3a. todo.md's checkbox is false — nothing of goal 3 increment A exists on disk — HIGH

Goal 3 A1 is marked `[x]` ("Migration 0031 — commitment_dependency … same migration adds
situation_doc"). Derivation: `grep -l situation_doc\|commitment_dependency
backglass/db/migrations/*.sql specs/schema.sql` → no match; `backglass/situation.py`
does not exist; the 0031 on disk is goal 4's `assignment`. The todo itself says "Goal 3's
planned 0031 renumbers to 0032" — and 0032 was never written. Schema is 31/31 applied.

So the state doc, the dependency table, the `logic_check` rebuild (partial unique for
supersession), the relevance-v2 `depends_on` field, and the invalidation pass are all
unbuilt, while the plan reads as started. Fix the checkbox to `[ ]` first — a plan that
lies about its own state is how the next session builds on air.

### 3b. The design as written is right — build it, in this order, with two constraints

The three stated assumptions hold up: deleted = tombstoned; the doc is a **versioned
rendering over `fact` + `context._situation`**, never a second store (ledger-stays-primary
would forbid anything else); `none` is a first-class dependency. Keep all three.

Order: 0032 (situation_doc + commitment_dependency + logic_check rebuild) → situation.py
`render()`/`save()` + `backglass situation` CLI → relevance v2 with `depends_on` required
on every verdict → B (invalidation, dry-run, would-drop list read row by row — the
2026-08-20 retraction near-miss is the governing lesson) → C (enable + backfill under the
cap).

Constraints that bite, both from lessons:
- **The scheduler runs this checkout.** Writing `0032_*.sql` into `backglass/db/
  migrations/` deploys it within 30 minutes and kills the installed sidecar
  (2026-08-21 lesson, proven on 0031). Author it in a worktree the scheduler does not
  run, and rebuild the sidecar in the same session it merges.
- **Name it `situation`, not `state`** — already decided (A6); `backglass state` means
  installation ground truth.

### 3c. One addition to the A-spec, cheap now and hard later

`render()` should exclude (or mark) commitments the staleness gate is currently holding
back — otherwise the doc's "active fronts" section re-asserts the exact rows the planner
was just taught to ignore, and the relevance model reads them as live. Same population,
one shared predicate (`staleness.py` already owns it).

---

## 4. The knowledge map (facts + context tiers)

What exists is good: `context.assemble` (long-term facts / people / situation) is
deterministic, capped, empty-when-empty, ledger-only, and wired into triage, batch
triage, and extraction. Fact writeback runs through the poison gate (proposed facts
invisible to `owner_context` until accepted). Numbers today: 41 active facts, 12 with
provenance, 2 proposed, 853 chars of owner_context.

### 4a. The situation section leads with the stalest items in the ledger — MEDIUM, cheap fix

`context._situation` selects open commitments with `substr(due_at,1,10) <= :horizon`
ordered ascending (`context.py:197–208`). Overdue rows satisfy `<= horizon`, so with 380
open (state) and ~60+ overdue (audit §4: oldest "Clean fishtank" due 2026-01-06), the
five lines every model call reads as "the owner's current situation" are the five
*oldest overdue* rows — precisely the population staleness holds out of the plan and
relevance is slowly retiring. The context block re-poisons every call with the backlog.

Suggestion: two buckets — overdue as a count plus at most one or two *recent* overdue
(due within, say, 14 days), then the nearest upcoming `today..horizon` rows as the list;
and exclude staleness-held ids via the shared predicate. Deterministic either way.

### 4b. The headline count the model reads is inflated by known duplicates

"380 open commitments" includes the duplicate families the scrub board surfaces (87 rows
flagged; 5× UT Dallas was one commitment). Until re-extraction resurrection is fixed
(product-gaps B, open: `resolve` does not tombstone the way `drop` does, so extraction
re-creates resolved work), the count in every prompt overstates load. Noted so nobody
tunes prompts around a number that is partly an artifact.

### 4c. Caps can rise where judgment depends on breadth

Cost tracks output, not input (2026-08-07 audit; restated in the todo's constraints).
`NOW_CHARS = 900` / `LINE_LIMIT = 5` / `PEOPLE_LIMIT = 8` are triage-shaped ceilings.
For extraction and especially the relevance judge, a fuller situation is nearly free and
directly improves verdicts — goal 3 A5 (feed `situation.render()` to the relevance
prompt) is the designed home for that; until then, a per-caller limit argument on
`assemble()` (triage keeps 900, extract/relevance get, say, 2–3×) is a two-line change.

### 4d. Review queue starves the map's honesty

170 of the open commitments sit below the 0.7 confidence threshold (audit §4) —
invisible to the planner by rule 2, but *visible* to the context counts and to
relevance. A queue nobody drains is a leak in both directions: the board undercounts
what the context overcounts. The scrub surface exists; it needs a drain habit or an
aging rule of its own (owner ruling, not code).

---

## Suggested order (across all four surfaces)

1. **Reboot the Mac** (§1d) — restores the 05:45 plan and 06:00 brief; no code.
2. **Surface the silent post-passes + unify the chain** (§1a, §1b) — small, restores
   rule 5 over the newest and most important passes.
3. **Fix `context._situation` ordering** (§4a) — cheap, improves every model call
   immediately.
4. **Conflict identity for immutable re-reads** (audit §2) — restores the exit-code
   signal everything else is read through.
5. **Correct goal 3 A1's checkbox, then build 0032 → situation.py → relevance v2 →
   invalidation** (§3) — in a worktree, sidecar rebuilt same session.
6. **Rebuild the sidecar now** (§1f) — it is 7 surfaces behind today.
7. Pass reordering (§1c), canvas-grace re-keying (§2c), context caps per caller (§4c)
   ride along with their neighbours.
