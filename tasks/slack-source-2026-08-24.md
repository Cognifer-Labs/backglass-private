# Slack as a live source (2026-08-24)

> Part of the 2026-08-24 session. Index, current state and the owner-blocked steps: `tasks/todo.md` §Handoff — model routing, Slack, triage (2026-08-24).

**Finding that reframes the request:** the Slack connector already exists and is fully
wired — `backglass/connectors/slack.py`, `Settings.slack_token` / `slack_channels`,
the `_all_connectors` gate, `detect.py`'s `NEEDS_SETUP` hint, docs/07 §Slack, and
`tests/test_slack.py` + the idempotency matrix. `audit_sources.py` reports wiring clean.

What is missing is (a) activation — no `SLACK_TOKEN` in `.env`, so the connector is
never constructed — and (b) the "efficient and better" half, which is where the real
work is.

**Ledger state, checked before touching item shapes:** 0 `source_item` rows under
`slack%`, no `credential` row, no `monitored_chat` rows for `slack`. Clean slate, so
`external_id`, `title` and `author` shapes are free to change without tripping the
0002 immutability trigger.

**Stated assumption:** working in the shared checkout rather than a worktree. Nothing
in the tree has been modified in 6 hours and only this session's PID holds the repo as
cwd, so the concurrency rule's precondition ("another session may be active") does not
hold. Milestones are snapshotted to the scratchpad (`git diff HEAD > slack-N.patch`)
after each green suite, per the 2026-08-05 lesson.

**Owner rulings taken 2026-08-24 (these overrule the connector's own docstring):**

1. User token (`xoxp-`) from a self-owned Slack app, not a bot token.
2. **Discovery + `/chats` allowlist page**, not env-listed raw IDs. This reverses the
   module docstring's "this connector never enumerates conversations". The docstring
   must be rewritten to say what changed and why, not silently contradicted.
3. **Track per-thread `latest_reply`**, closing the documented gap where a reply
   landing today on a thread started before the cursor is never read.

## Increments

Each lands tested and committed before the next starts.

- [x] **1. Per-channel + per-thread cursor map.** Today one `ts` watermark covers every
      channel and only advances after a fully clean pass over all channels *and* every
      thread fan-out; on `SlackRateLimited` the cursor is reset to where the run began.
      Under Slack's non-Marketplace tier (1 req/min on `conversations.history`, recorded
      in tasks/todo-archive-2026-08-01.md:495) a backfill never completes a clean pass,
      so the watermark pins at the start forever and every run re-reads from zero. That
      is the actual efficiency defect.

      Cursor becomes a versioned JSON document: `{"v": 1, "channels": {id: ts},
      "threads": {"<channel>:<parent_ts>": ts}}`. A bare `ts` string (what an existing
      install would hold) is read as a global floor for every channel — backward
      compatible, and the only migration path a cursor gets.

      Partial progress persists: a channel that finished advances even when a later
      channel is rate-limited.

- [x] **2. Honour `Retry-After`.** `_http` currently maps HTTP 429 to
      `{"ok": false, "error": "ratelimited"}` and discards the header; `BACKOFF_SECONDS`
      only covers 5xx. Read `Retry-After`, sleep it under a bounded per-run wait budget
      (`SLACK_RATE_LIMIT_BUDGET_SECONDS`, default 120), stay serialized, and give up
      cleanly into the same `SlackRateLimited` path once the budget is spent. Correct
      under any tier, so it does not depend on measuring the owner's tier first.

- [x] **3. Discovery + allowlist.** `users.conversations` (types
      `public_channel,private_channel,im,mpim`) enumerates what the owner is a member
      of; each becomes a `Sighting` on `monitored_chat` under source `slack`. Nothing is
      read until it is chosen on `/chats` — the same contract iMessage and Instagram
      already have.

      Discovery runs on every fetch and is **not** gated by the allowlist: the
      2026-08-03 lesson is exactly this cycle (a page whose input comes from the thing
      the page disables stays blank forever).

      `SLACK_CHANNELS` still works — `chats_mod.seed_from_env` turns existing raw IDs
      into decided `monitor` rows on first sync.

      Keys are channel IDs (`C…`/`D…`), never names: a channel rename must not silently
      un-monitor a conversation. Display names are what the page shows.

- [x] **4. Per-thread `latest_reply` watermarks.** A parent with `reply_count > 0`
      registers its thread in the cursor's `threads` map. Subsequent runs re-poll every
      registered thread with `oldest = <that thread's watermark>`, independent of
      whether the parent still falls inside the channel window. Bounded by
      `SLACK_THREAD_WINDOW_DAYS` (default 30) so the map cannot grow without limit —
      threads with no activity inside the window are dropped from it.

- [x] **5. Readable provenance.** `title` is `f"#{channel_id}"` today, so rule 1's
      provenance line in the brief reads `#C0123ABC`. Resolve channel and user display
      names once per run and cache in-connector, falling back to the raw ID when
      resolution fails — a name that cannot be resolved is never a reason to drop an item.

      *Built differently from the plan, and better:* no `conversations.info` call is made
      at all. `users.conversations` already returns each conversation's name in the
      discovery pass, so channel names cost zero extra requests, and `users.list` is
      called lazily — once per run, and only when a DM or an author actually needs a name.
      Under a 1-request-per-minute tier that difference is the whole feature.

      **The hazard this exposed, which the plan had not seen:** a display name is not
      deterministic given an item's id. Hashing it would recompute a different
      `content_hash` for an already-stored row after a rename or a failed `users.list`,
      which is precisely the `canvas:ics` immutability failure now in the audit output.
      So `content_hash` is computed over the raw channel and user IDs while the fields
      carry the readable form.

      `backglass/extract/entities.py` maps nothing from a raw `U…` id, so the docstring's
      claim that "the entity resolution layer maps identifiers to people anyway" is
      decoration. Resolving at ingest is what makes it true.

- [x] **6. Subtype filter is over-broad.** The blanket `if message.get("subtype")`
      drops `thread_broadcast` (a genuine human reply sent also to the channel) and
      `file_share` (which carries the owner's message text alongside the file). Invert
      to a noise blocklist keyed on the subtypes that are actually machine chatter.

- [x] **7. Activation runbook + docs.** Rewrite the module docstring for rulings 2 and
      3, update docs/07 §Slack, `.env.example`, and `docs/13-activation-runbook.md` with
      the exact app-creation steps and the User Token Scopes the connector needs:
      `channels:history`, `groups:history`, `im:history`, `mpim:history`,
      `channels:read`, `groups:read`, `im:read`, `mpim:read`, `users:read`.

## Test gate (the skill's four, plus the new behaviour)

- Fixture-backed fetch, idempotency (two runs, zero writes), health both ways, boundary
  — all four already exist and must stay green.
- New: `Retry-After` honoured and the budget bounded; a completed channel's cursor
  advances while an interrupted channel's pins; a legacy bare-`ts` cursor is read as a
  floor; a late reply to a pre-cursor thread is picked up; `thread_broadcast` and
  `file_share` survive the filter while `channel_join` does not; discovery records
  sightings for conversations the allowlist rejects.
- Each new test proven by mutation, counting mutations against failures per the
  2026-08-02 lesson.

## What actually shipped

All seven increments, plus one defect the plan had not predicted.

**Found by smoke-testing the real registry, not by any test:** `config._csv` lowercases
every comma-separated setting, and `slack_channels` was in that list. Slack conversation
IDs are case-sensitive API identifiers, so `SLACK_CHANNELS=C0FOUNDERS` reached
`conversations.history` as `c0founders` and would have come back `channel_not_found`.
This predates today's work — the old registry passed the same lowercased tuple straight
to the API — and it never surfaced because the allowlist comparison casefolds both sides,
so the value looked right everywhere except at the wire. `slack_channels` now has its own
case-preserving validator, pinned by a test read from the environment the way production
reads it.

**Verification:**

- 2,475 tests green; `ruff` and `mypy` clean on every file touched.
- Ten mutations against `slack.py`, each caught by the test written for it. The first
  mutation pass reported ten catches that were an artefact — `pyproject`'s
  `addopts = "-x"` stopped every run at the first failure, so each mutation appeared to
  be caught by whichever test happened to run first. `--maxfail=999` is what made the
  attribution real, and the corrected pass is what the claim above rests on.
- The config fix proven red by deleting the validator outright and watching the test fail.
- Real-wiring smoke on a temp database (never `data/backglass.db`): the connector is
  constructed by `_all_connectors`, `SLACK_CHANNELS` seeds `monitored_chat`, true case
  survives, and saying yes on /chats NULLs `slack:personal`'s cursor so the conversation's
  history is re-read rather than only its future.

**Not verified, and cannot be here:** no call has been made against a real workspace,
because there is no token yet. What a live run will confirm that fixtures cannot: the
actual rate tier, whether `users.conversations` returns the mpim shapes assumed, and
whether any scope is missing from the list in docs/07.

## Handoff — open items

- [ ] **S8. Owner: mint a Slack user token.** api.slack.com/apps → Create New App → From
      scratch → pick the workspace → OAuth & Permissions → **User** Token Scopes (not the
      bot column): `channels:history`, `groups:history`, `im:history`, `mpim:history`,
      `channels:read`, `groups:read`, `im:read`, `mpim:read`, `users:read` → Install to
      Workspace → copy the `xoxp-` token into `SLACK_TOKEN`. Leave `SLACK_CHANNELS` empty.
      A bot token (`xoxb-`) authenticates and is the wrong instrument: no DMs.
- [ ] **S9. `uv run backglass doctor`** — confirms `auth.test` passes before a sync spends
      anything on a bad token.
- [ ] **S10. One sync, then choose on /chats.** Every conversation appears undecided;
      nothing from Slack is read until it is set to `monitor`. Saying yes rewinds the
      cursor so that conversation's recent history is read, not only its future.
- [ ] **S11. First-run reality check.** Nothing here has touched a real workspace, so
      three things are assumed rather than measured: the actual rate tier, the `mpim`
      payload shape, and whether the scope list above is complete. Watch the first sync's
      errors and `deferred_work`.

## Out of scope, flagged not absorbed

`audit_sources.py` reports **81 pre-existing failures** that have nothing to do with
Slack, in two families:

1. OpenRouter free-tier HTTP 429 (`free-models-per-day`, limit 50/day) killing triage
   and extract across ~120 items. Matches the memory note that the free-tier backend
   flaps.
2. `content changed for an immutable source_item: canvas:ics:assignment:78330xx` — the
   Canvas ICS connector is recomputing a different `content_hash` for items it has
   already stored. That is the 2026-07-30 external_id/determinism lesson recurring and
   is a **real bug worth its own session**.

Also unresolved: `notes:obsidian` is authorized and healthy but has never stored an item.
