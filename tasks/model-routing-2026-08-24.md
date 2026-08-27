# Outsourcing the model work (2026-08-24)

> Part of the 2026-08-24 session. Index, current state and the owner-blocked steps: `tasks/todo.md` §Handoff — model routing, Slack, triage (2026-08-24).

## The request, and the premise that did not survive it

The ask was to move the model work somewhere that can actually carry it, with GitHub
Education models as the intended destination.

**GitHub Models was fully retired on 2026-07-30** — three and a half weeks before this
session. docs.github.com states it plainly: "the playground, model catalog, inference API,
and bring your own key (BYOK) are no longer available to any customer." Verified against
two separate pages. There is no version of the original plan that works, and GitHub's own
retirement notice points at Azure AI Foundry or Copilot instead.

For completeness, since it is the obvious next thought: **Copilot is not a substitute.**
A free Copilot Pro seat through GitHub Education is real, but Copilot is an IDE product,
and driving its endpoints as a general inference API is outside its terms. Not recommended
and not built.

**Owner's ruling once told:** "use whatever is free, then fall back on Anthropic models",
and later, overriding an earlier answer in the same session: **no local inference — the
machine is not powerful enough.** The local-triage branch was abandoned on that word and
the model download cancelled before anything landed on disk.

## The real diagnosis: it was never a model problem

`MODEL_FALLBACKS` has been treated as the answer to free-tier flakiness, and it is the
wrong instrument. It is a list of *models* handed to one gateway — OpenRouter routes the
request itself when the body carries a `models` array. That helps when a model is busy.
It cannot help when **the gateway** refuses, and on this account the gateway refuses daily:

- `GET /api/v1/credits` → `{"total_credits": 0, "total_usage": 0}`. Pure free tier, never
  billed. (The `$450` in my own memory note was Backglass's internal
  `monthly_spend_cap_cents`, not the provider balance — corrected.)
- Free tier = **50 requests/day** (`X-RateLimit-Limit: 50`).
- Arrivals, measured over 2026-08-11 → 08-24: **50–276 items/day**, median ~100.

Triage batches 12 items per call, so the steady-state need is tens of calls a day against
a 50/day ceiling — right at the wall, which is why it works some days and collapses others.

## The defect underneath, which mattered more than the capacity

`RateLimited` was raised in `ClaudeCLIBackend` and **nowhere else**. Every hosted
configuration — `openai_compatible`, `deepinfra` — runs through `DeepInfraBackend`, where
an HTTP 429 became an ordinary `ModelError`. Three consequences, all visible in the
2026-08-24 audit:

1. **A cascade.** A rate-limited *batch* was indistinguishable from a failed batch, so all
   twelve of its items escalated to per-item calls, each of which 429'd again. The ~130
   individual `triage 10604…` errors in the audit are one shut window amplified twelvefold.
2. **`_LimitClaim` never fired.** The run kept calling a provider that had already answered
   `X-RateLimit-Remaining: 0`, instead of stopping and leaving the items pending.
3. **`degrade_reason` stayed empty.** The owner saw a wall of errors rather than "today's
   quota is spent, the items are still pending and tomorrow's sync takes them."

`tasks/lessons.md` had flagged this: the 2026-08-03 spend-cap entry ends "rate-limit half
still open". This was that half.

A second, smaller instance of the same shape: `_looks_rate_limited`'s marker list spells it
`rate limited`, while `DeepInfraBackend.fallbacks`'s own docstring quotes OpenRouter
answering `429 … temporarily rate-limited upstream`. The one provider message this codebase
documents verbatim was the one message the matcher could not see.

## What shipped

- [x] **429 → `RateLimited` on the hosted backend**, keyed on the transport status rather
      than a phrase — at that layer 429 cannot be a substring of a log line, which is the
      ambiguity the `cli.js:1:429517` comment exists to guard.
- [x] **200-with-error envelopes read too.** OpenRouter answers 200 with an error object
      when an upstream refuses rather than the gateway; that used to fall through to "no
      usable tool call", the generic failure that escalates and re-fails.
- [x] **`rate-limited` added to the marker list.** Deliberately the past participle rather
      than folding hyphens across the whole string: folding also matches a traceback frame
      like `rate-limit-handler.js:12`, which is the `cli.js:1:429517` mistake wearing
      different punctuation. A false limit stops the wave while spending no attempt — the
      shape that stalls a queue forever.
- [x] **`Failover`** — a *provider* chain. Primary refuses (shut window, dead credentials,
      no usable tool call) → the call crosses to the secondary. Sits **inside**
      `AuthCircuit`, deliberately: a dead OpenRouter key is not a reason to stop the run
      when the secondary is a working CLI session.
- [x] **Model names are remapped as the call crosses.**
      `MODEL_TRIAGE=nvidia/nemotron-nano-9b-v2:free` means nothing to the Claude CLI.
      Keyed off the settings that produced the name, not off the schema — `TriageRouter`'s
      `"keep" in properties` test is blind to the *batch* triage shape, whose properties
      are `{"items": …}`.
- [x] **An imputed secondary's price is not charged to the cap.** `claude_cli` reports an
      API-equivalent price for a subscription call nobody was billed for. Summing it is
      exactly the 2026-08-03 failure where nine consecutive syncs degraded to triage-only
      over $20.06 that never existed; the fallback path was a fresh way to reach it.
- [x] **The secondary never inherits `MODEL_API_KEY`.** That key belongs to the primary and
      the secondary is a different provider by construction — handing OpenRouter's key to
      Anthropic buys a 401 on every call of the outage the chain exists to cover, and it
      reads as broken rather than unconfigured. Found because a test *skipped* instead of
      passing.
- [x] **A fallback that cannot be built does not take the run down** (rule 5), and the run
      report says how many calls crossed over and why the first one did — otherwise "the
      free tier has been dead for a week" and "everything is fine" look identical.
- [x] Live `.env` set to `MODEL_FALLBACK_BACKEND=claude_cli`, `.env.example` documented.

## Verification

- 2,500 tests green; `ruff` clean on every file touched. `mypy` reports 8 errors in
  `client.py`, all pre-existing at HEAD in `AnthropicAPIBackend` and none of them mine.
- **11 mutations, 11 caught, each by the test written for it** — including reverting the
  429 branch, restoring the hyphen fold, un-remapping the model name, charging the imputed
  price, and flipping the `AuthCircuit` nesting.
- The live config builds the intended chain, checked by constructing it:
  `AuthCircuit(Failover(DeepInfraBackend@openrouter → ClaudeCLIBackend))`, remap
  `{nemotron-nano → haiku, nemotron-3-super → sonnet}`, secondary flagged imputed.
- End-to-end sync run against a **copy** of the ledger, never `data/backglass.db`.

## Still open

- **Capacity is not solved, only survivable.** The chain means a shut free tier no longer
  loses work; it does not make the free tier bigger. If the Claude subscription is doing
  most of the lifting every day, the honest fixes are $10 on OpenRouter (→ 1000 free
  requests/day, the provider's own offer in the 429 body) or Azure for Students' $100
  credit. `report.fallback_calls` is the number that tells you which world you are in.
- **Embeddings still run locally** on Ollama (`nomic-embed-text`, 274 MB) at
  `EMBEDDING_BASE_URL`. Left alone deliberately: it is a 137M-parameter embedding model,
  not inference, and it is the only reason the drop folder's contracts and letters can be
  indexed without leaving the machine (docs/08). Say the word and it moves.
- The `canvas:ics` immutable-content conflicts flagged in the Slack session are still
  unfixed and unrelated to any of this.
