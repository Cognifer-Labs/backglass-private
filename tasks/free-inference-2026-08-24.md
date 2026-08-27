# Free inference capacity (2026-08-24)

> Part of the 2026-08-24 session. Index, current state and the owner-blocked steps: `tasks/todo.md` §Handoff — model routing, Slack, triage (2026-08-24).

Follow-on from `tasks/model-routing-2026-08-24.md`, which made a shut free tier survivable
but did not make it bigger. The measured gap: ~123 model calls a day against OpenRouter's
50/day free ceiling, with **53 of 123 calls in the last probe run crossing over to the
Claude subscription**. This is the search for capacity that is actually free.

## Recommendation: Groq

One provider covers the need with ~117× headroom, and it is better than the current
primary on privacy as well as on volume.

| | OpenRouter free (today) | **Groq free** | Cerebras free | Gemini free |
|---|---|---|---|---|
| requests/day | **50** | **14,400** ᵗ | — (1M tokens/day ᵗ) | 250–1,500 ᵗ |
| requests/min | — | 30 ᵗ | 30 ᵗ | 10–30 ᵗ |
| tokens/min | — | 6,000 ᵗ | 60–100k ᵗ | 250k ᵗ |
| context | model-dependent | 131k | **8,192 (free-tier cap)** ᵗ | large |
| credit card | no | no | no | no |
| trains on inputs | provider-dependent, opt-out is a setting | **contractually no** | unverified | **yes — explicitly** |
| OpenAI-compatible | yes | yes | yes | yes (`/v1beta/openai/`) |

ᵗ **Third-party reported, verify at signup.** Groq's own rate-limit page renders its table
only for a logged-in account (`console.groq.com/settings/limits`), so every number in the
volume rows above comes from secondary sources and must be confirmed against the account
once it exists. The privacy rows below are primary-source.

### Why Groq, in order of what matters

1. **Privacy, and it is the deciding factor.** This pipeline sends whole emails and
   iMessages through triage. Groq's Services Agreement §4.2 is unambiguous, and it is
   primary-source: *"Groq is not permitted to use Inputs or Outputs for training or
   fine-tuning any AI Model Services or other models, unless explicitly granted permission
   or instructed by Customer."* Its data page adds *"By default, Groq does not retain
   customer data for inference requests"* — temporary logs only for reliability or abuse
   investigation, up to 30 days, with a zero-data-retention opt-out. No free/paid split.

   That is strictly better than the status quo. OpenRouter's own docs say each provider
   has its own policy and that there are *separate settings for paid and free models* for
   whether to allow routing to providers that may train — i.e. free traffic can reach a
   training provider unless that toggle is set. `.env` already carries a warning about it.

2. **Volume.** 14,400/day against a need of ~123 ends the capacity question outright.
3. **No code change.** Groq is OpenAI-compatible, so it is `MODEL_BASE_URL` and a key. The
   two OpenRouter-only body fields — the `models` array and `usage: {include: true}` — are
   gated on `"openrouter" in self.base_url` (client.py:453, 466), so Groq never receives
   them and cannot 400 on an unknown field. Groq returns no cost field, which lands as the
   honest `0.0` (client.py:540–543), not an invented number.

### Ruled out, not offered as choices

- **Gemini free tier — disqualified on rule 6.** Google reserves the right to use free-tier
  inputs and outputs to improve its products, *including human review*, and its own
  documentation says not to send confidential or personal information to unpaid services.
  This ledger is private correspondence. Not a trade-off; a disqualification. The paid tier
  does not carry this, and would be a different conversation.
- **GitHub Models — retired 2026-07-30.** See `tasks/model-routing-2026-08-24.md`.
- **Cerebras — backup, not wired.** Generous on tokens, but the free tier caps context at
  8,192 tokens, which is below what extraction of a long mail thread needs. Fine for
  triage, wrong as a single answer.

## What this search turned up about the current setup

**`MODEL_TRIAGE=nvidia/nemotron-nano-9b-v2:free` no longer exists.** Asked for by itself it
returns `HTTP 404: No endpoints found`. It is not in OpenRouter's catalogue at all; the
other three configured ids still are.

It has been invisible because the pipeline sends OpenRouter a `models` array, so the
gateway silently routes past the dead primary to a fallback and returns 429 only once the
daily quota is gone. The array is doing exactly its job and, in doing it, hid the fact that
the model this installation names for its highest-volume tier has been gone. Nothing in the
logs distinguishes "your triage model died" from "the free tier is busy" — which is the
same silent-fallback shape as the `models`-vs-provider confusion this whole thread is about.

Currently free **and** tool-capable on OpenRouter: 14 models (down from 16 on 2026-08-21).

### And the measurement that settles it: 0 of 14

Running the probe over every one of those 14, with the triage schema, 2026-08-24:

- **12 returned HTTP 429**, each carrying `X-RateLimit-Limit: 50`.
- **2 returned HTTP 403** — `thinkingmachines/inkling:free` and `inkling-small:free` are
  *"only available on agentic harnesses"*, so they are in the catalogue as free and
  tool-capable and are not usable from a pipeline at all.
- **0 produced a valid forced tool call.**

**The daily quota is account-wide, not per-model.** The same `X-RateLimit-Limit: 50` came
back from all twelve, so once the day's 50 are spent, *every* free model on OpenRouter is
spent with them.

That falsifies the premise `MODEL_FALLBACKS` was configured on. `.env` says of it: *"Tried
in order by OpenRouter itself when the primary will not serve. Five of sixteen free models
answered 429 within the same minute six others served, so this is what makes the free tier
usable."* That observation was real, and it was about **per-minute upstream** throttling,
where rotating models genuinely helps. Against the **daily account cap** rotation buys
nothing — there is no other model to route to, because the limit is not attached to the
model. The setting has been read as insurance against a failure mode it cannot cover.

So at the time of writing the pipeline is running **entirely** on the Claude subscription,
and the only reason that is not visible as an outage is the failover chain built earlier
today. `fallback_calls: 53` was not a bad afternoon; it is the steady state.

## Applied while waiting for the key

The Groq move needs an account only the owner can create, but two things in the current
config were wrong independently of it and were fixed:

- **`MODEL_TRIAGE` pointed at a dead model.** Now `nvidia/nemotron-3-super-120b-a12b:free`
  — the id `MODEL_EXTRACT` has been running successfully, so it is the one model this
  installation has actually proven, and a model that passes the 100-field
  `CommitmentExtraction` schema passes the 2-field `TriageVerdict` a fortiori. Explicitly a
  placeholder for a measurement, not a measurement: the free quota resets at 00:00 UTC
  (17:00 Phoenix, ~4h after this was written), after which `--triage` can choose a cheaper
  slot-1 model on evidence.
- **`MODEL_FALLBACKS` reordered** so all three routed slots hold distinct live models.
  Before: `[dead, super, north]` with `ultra` truncated off the end and unreachable. After:
  `[super, north, ultra]`, verified against the live catalogue.

Both are interim. Neither changes the arithmetic: 50 requests a day against ~123 needed.

## Prepared, not applied

Nothing was written to the live `.env`. Pasting a Groq key is itself the decision to route
mail text to a new third party, so that stays the owner's.

1. Sign up at `console.groq.com` — GitHub or Google login, no card. Create an API key
   (`gsk_…`). While there, read the real limits at `console.groq.com/settings/limits` and
   correct the ᵗ rows above.
2. **Probe before configuring.** `scripts/openrouter_shootout.py` now takes any
   OpenAI-compatible provider, and it sends the genuine schema through the genuine forced
   `tool_choice` — which is the thing a catalogue's `tools` flag does not prove:

   ```
   uv run python scripts/openrouter_shootout.py --triage \
       --base-url https://api.groq.com/openai/v1 --api-key gsk_... \
       --model llama-3.3-70b-versatile --model llama-3.1-8b-instant \
       --model openai/gpt-oss-120b

   uv run python scripts/openrouter_shootout.py \
       --base-url https://api.groq.com/openai/v1 --api-key gsk_... \
       --model llama-3.3-70b-versatile --model openai/gpt-oss-120b
   ```

   The second run uses the full `CommitmentExtraction` schema — 100+ fields, nested objects
   and enums — and is the harder test. Take the extract model from what passes *that*.
3. Then, in `.env`, with model ids taken from the probe rather than from this document:

   ```
   MODEL_BASE_URL=https://api.groq.com/openai/v1
   MODEL_API_KEY=gsk_...
   MODEL_TRIAGE=<a model that passed --triage>
   MODEL_EXTRACT=<a model that passed the extract run>
   MODEL_FALLBACKS=                 # OpenRouter-only mechanism; blank it off Groq
   MODEL_FALLBACK_BACKEND=claude_cli # keep — the net under the new primary
   ```

   `EMBEDDING_BASE_URL` stays on Ollama regardless: `nomic-embed-text` does not exist on
   Groq and the 1,248 stored vectors cannot be compared against another model's space.
4. Watch `fallback_calls` on the next few runs. It should go to ~0. If it does not, the
   probe picked a model that fails on real mail rather than on the fixture, and the run
   report is what says so.

## Handoff — open items

- [ ] **F1. Owner: create a Groq account** at `console.groq.com` (GitHub login, no card),
      mint a key, and read the real limits at `console.groq.com/settings/limits` — then
      correct the ᵗ rows in the table above, which are third-party numbers.
- [ ] **F2. Probe before configuring.** Both commands in §Prepared. Take the model ids from
      what passes, not from this document.
- [ ] **F3. Switch `MODEL_BASE_URL`/`MODEL_API_KEY`/`MODEL_TRIAGE`/`MODEL_EXTRACT`,** blank
      `MODEL_FALLBACKS` (OpenRouter-only mechanism), keep `MODEL_FALLBACK_BACKEND=claude_cli`
      as the net. `EMBEDDING_BASE_URL` stays on Ollama regardless.
- [ ] **F4. Watch `fallback_calls` for a few runs.** It should fall to ~0. If it does not,
      the probe picked a model that passes the fixture and fails on real mail, and the run
      report is what says so.
- [ ] **F5. Re-measure the interim `MODEL_TRIAGE`** after 00:00 UTC — it is a placeholder
      chosen without measurement because the quota was spent. Also revert
      `MONTHLY_SPEND_CAP_CENTS` from `45000` to `5000` on 2026-09-01.

## Deliberately not built

An N-provider chain. `Failover` nests structurally if it is ever wanted, but one provider
covers the need a hundred times over, nobody asked for one, and the parallel-comma-list
config it would need is the length-mismatch shape this repo's lessons already warn about.

`Retry-After` sleeping inside `DeepInfraBackend` for Groq's 30 RPM / 6k TPM burst limits.
A burst 429 crosses to the CLI through the existing chain — correct, if not optimal — and
it cannot be tuned without a key and real traffic. Revisit if `fallback_calls` stays high
for burst reasons rather than quota reasons.
