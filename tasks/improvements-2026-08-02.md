# Backglass improvement plan — 2026-08-02

Synthesized from a 4-scout state map + 4-lens idea panel (daily-value, extraction,
reliability, surface-growth), cross-checked against tasks/audit-2026-08-01.md and the
current code. Ideas from the panel that turned out already fixed were dropped after
direct verification: spend-cap wave enforcement (sync.py:404-480), confidence-gated
supersession (extract/commitments.py:158-165), reminders upstream watermark, db/.env
chmod 600, web security middleware (web/security.py), rollover/weekly confidence
predicates, per-item extraction transactions.

Current live state (verified today): launchd has `com.backglass.plan` +
`com.backglass.plan-catchup` loaded; **sync, brief, and shutdown are not scheduled**.
Credential table has only apple-notes + reminders healthy; imessage failed; Gmail /
Calendar / Drive / Canvas never authed. No backup of `data/backglass.db` exists.
`BOUNDARY_MODE` unset in live `.env`. App root still `~/Downloads/backglass`.

---

## Tier 1 — Finish going live (highest lived-value per hour; mostly wiring, not code)

1. **Relocate + schedule.** Move the app root out of `~/Downloads` to a stable path,
   re-render and load sync/brief/shutdown via `backglass schedule install`, confirm
   with `doctor`. Until this lands, the 06:00 brief — the product's core promise —
   doesn't happen. (audit #22/#24; templates + install command already exist)
2. **Backups.** `backglass backup` using `VACUUM INTO` (WAL-safe) to a rotated
   snapshot dir outside the repo (7 daily + 4 weekly), an eighth launchd template
   `com.backglass.backup`, a doctor check failing when newest snapshot > 48h, and
   `backglass restore <snapshot>` with `PRAGMA integrity_check` before swap.
   `data/backglass.db` is the only copy of the real ledger since 2026-07-30. (audit #23)
3. **Activate the client-data boundary.** Set `BOUNDARY_MODE` in the live `.env`
   (owner decision: populate denylist vs explicit `full_scope`), run `purge-boundary`
   once, and add a doctor check that FAILS when work-adjacent sources are enabled with
   the mode unset. Legal-weight rule 6; machinery exists, deployment inert. (audit #18)
4. **Widen intake = run the activation runbook, not write connectors.** All 14
   connectors exist in code; the ledger reads two. Reset the failed imessage
   credential, auth the Google trio (docs/07 OAuth section), add a Canvas token.
   Add a doctor line listing built-but-never-authed connectors so the gap stays
   visible. (docs/13, docs/09 Phase 5 exit criterion)

## Tier 2 — Silent-failure visibility (the failure mode Tier 1 creates)

5. **Heartbeat staleness, one query, three surfaces.** Expected-cadence map over the
   `run` table (sync ≤ 2× interval, brief/plan daily by their windows) driving:
   (a) vermilion sidebar alert "Last sync ran Nh ago" + gold "No plan for today"
   in `web/panels.py` (pattern at :346-366); (b) doctor FAIL on overdue jobs;
   (c) a provenance-linked line in the morning brief itself ("Ledger last updated
   26h ago — Gmail failing since Thu") — the brief is the daily dead-man's switch.
   Currently a dead launchd job produces a green dashboard and a confident stale brief.
6. **Run history page.** `GET /runs` over the existing `run` table (job, duration,
   items in/out, spend, degraded badge, expandable `errors_json` — currently rendered
   nowhere), linked from the Sources panel's last-run line. Optional 30-day spend
   sparkline within the three-series chart limit.

## Tier 3 — Extraction learning loop (the compounding one)

7. **Aggregate rejection reasons + confidence calibration.** The four reject-reason
   buttons store typed reasons and accept() preserves model confidence precisely so a
   distribution can be computed — nothing computes it. Add `backglass noise
   rejections`: reason × sender × prompt-version distribution + model-score vs
   owner-verdict calibration table + one-line diagnosis (wrong_date → dates.py,
   not_a_commitment → triage prompt). (web/actions.py:26-146)
8. **Harvest owner corrections into eval fixtures.** `backglass evals harvest`:
   accepted/rejected commitments → scrub-gate-redacted fixture JSONs in the existing
   `tests/fixtures/commitments/` format, owner verdict as gold label. Grows the eval
   set from 7 hand-written fixtures to real-traffic coverage. Highest-leverage
   extraction idea in the panel.
9. **Persist eval results per prompt stamp + `--compare`.** Eval runs currently print
   and exit; store JSON records in `evals/results/` and diff against the previous
   prompt version. Evals still never gate CI.
10. **Replay backend (planned L7).** Record (prompt-hash, model, response) during live
    syncs; `--replay` on eval scripts replays free. Makes "run evals after every
    prompt edit" economically trivial — precondition for 7-9 compounding.
11. **Date parser gaps.** Month-name dates ("March 3", "Jan 5th"), December→January
    year rollover in `_guard`, time-of-day ("by 5pm Friday"). All resolve to
    None-with-note today. Fixtures for both timezone directions per house rule.

## Tier 4 — Remaining correctness debt (audit residue, verify-then-fix)

12. **Connector cursor discipline + idempotency parametrization.** Verify which of
    the audit's cursor bugs survived commit 61d0388 (gmail per-item abort #8,
    canvas `<=` #9, instagram fixed-window #10 — reminders confirmed fixed), fix the
    remainder via a shared watermark helper in `connectors/base.py`, and parametrize
    `tests/test_idempotency.py` across all 14 connectors (currently 1/14 vs
    load-bearing rule 3).
13. **Brief feedback links.** "Spot on" / "Something's off" footer links → POST-backed
    confirm page writing the vestigial `brief.feedback` column (POST, not GET — the
    tracking-pixel lesson).
14. **Schema hygiene.** Refresh 9-tables-stale `specs/schema.sql` to migrated reality;
    resolve the `user_id`-on-every-table claim (add columns or amend the doc).
    (audit #19/#20)

## Tier 5 — Public-repo health (small, bounded)

15. **CI.** One `.github/workflows/test.yml`: uv sync + pytest on 3.11/3.12, ~25
    lines; add `.github/` to release ALLOW_PATHS. Evals stay out per locked decision.
16. **CONTRIBUTING.md + SECURITY.md one-pagers.** Point at CLAUDE.md's decisions
    table, fixture-not-live-API testing, anti-framework stance; private disclosure
    path for the localhost-only web surface. No governance boilerplate.
17. **Incremental releases.** `build_public_repo.py` currently orphans a fresh root
    commit each run; change the final stage to commit on top of the public remote's
    history (clone public only, never private) with a `release/YYYY-MM-DD` tag and
    parameterized message. Keep allowlist + scrub gate + in-tree pytest untouched.
18. **Real per-call cost rows.** `model_call` table written at `SpendCap.charge()`
    (the single choke point); rewrite `costs.py` summaries over real rows, estimates
    only for pre-migration history. Enables per-stage budgets. (costs.py:9 admits
    estimates)

---

**Suggested sequencing:** Tier 1 items 1-3 this week (they're wiring); item 4 as its
own session (OAuth churn); Tier 2 #5 immediately after Tier 1 (going live without the
dead-man's switch recreates the silent-stale problem); Tier 3 as the standing
background loop; Tiers 4-5 batched opportunistically.

**Also pending:** uncommitted plan-catchup work in the tree (8 files) — commit before
anything else touches `__main__.py`/`planner.py`.
