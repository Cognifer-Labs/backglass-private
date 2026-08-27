# Better triage — a plan grounded in what the ledger actually shows (2026-08-24)

> Part of the 2026-08-24 session. Index, current state and the owner-blocked steps: `tasks/todo.md` §Handoff — model routing, Slack, triage (2026-08-24).

Triage is 85% of this pipeline's volume: 10,757 items, 9,131 dropped, 1,612 kept. It is
where the model budget goes and where a mistake is unrecoverable — `triage.md` states the
asymmetry plainly, a false negative loses a commitment permanently while a false positive
costs one extraction call.

Everything below is measured against `data/backglass.db` and `model_call`, read-only.
Nothing here is implemented; this is for approval first.

## Escalation: real, but mostly a symptom of the provider, not the prompt

```
tier            outcome     calls    avg prompt   avg duration
triage_batch    ok             67       6,727ch      87,304ms
triage          ok            525       3,885ch      32,797ms
triage          error       1,179       4,109ch         853ms
triage_batch    error          38       9,292ch      15,838ms
```

**A first pass got this wrong and the correction is the point.** Dividing all-time
per-item calls (525) by all-time batch calls (67) gives 7.8 and suggests batching is
barely working. That number is junk: it pools three populations. 267 of those per-item
calls came from **139 runs that made no batch call at all** — the steady state is 0–6 new
items per sync, below `triage_batch_min = 4` — plus everything from before batching
existed.

Within runs that actually contain a batch call: **3.85 per-item calls per batch.** So 12
items cost `1 + 3.85 ≈ 4.85` instead of 12 — a **60% saving**, against the ~92% the design
implies. Worth closing, not a crisis.

And the likely cause is not model hedging:

- **36% of batch calls fail** (38 of 105). A failed batch escalates its *entire* chunk by
  design — "a failed batch proves nothing about its items".
- 38 failures × ~12 items ≈ **456 items escalated by failure alone**, against only 301
  per-item calls actually made in those runs. Failures can account for more than all the
  escalation observed.
- The outliers say the same thing. Run 391: one batch failed, 60 per-item calls followed.
  Run 599 (today, with the failover chain live): 2 batches, 0 failures, 17 escalations.

Those batch failures are the 429s and the dead `MODEL_TRIAGE` documented in
`tasks/free-inference-2026-08-24.md`, both fixed today. **So the honest expectation is that
escalation falls a long way on its own**, and instrumenting the prompt now would be tuning
against noise.

- [ ] **T1. Re-measure escalation once batches stop failing.** Same query, restricted to
      batch-bearing runs, after a few clean syncs. If the ratio is near 1–2, this section
      is closed and nothing else here is needed.
- [ ] **T2. Only if it stays high: record *why* each item escalated.** `BatchOutcome.escalate`
      pools three different causes — the model marked an item `uncertain`, the id was
      missing from the response, or it came back twice with disagreeing verdicts — and
      nothing records which. They need opposite fixes: hedging is a prompt/excerpt-length
      problem, while missing or duplicated ids mean the model cannot reliably emit 12
      structured verdicts and the batch size should come down instead. Instrument before
      choosing; guessing between them is how the wrong lever gets pulled.

## Second: 69% of per-item triage calls are failing

`triage error 1,179` against `triage ok 525`. The 853ms average on the failures is the
signature of a fast refusal, not a slow model — these are the 429s and the 404 from a dead
`MODEL_TRIAGE` documented in `tasks/free-inference-2026-08-24.md`.

This is already addressed by today's failover chain and the model-id repair, and it is
listed here because it distorts every other number: any triage quality measurement taken
before the error rate comes down is measuring the provider, not the prompt.

- [ ] **T3. Re-measure after the free quota resets (00:00 UTC).**
      `uv run python evals/eval_triage.py` for precision/recall, and
      `scripts/openrouter_shootout.py --triage` to choose a slot-1 model on evidence.
      Read the recall column first, per the eval's own docstring.

      **Two traps, both created today.** `eval_triage.py` builds its client through
      `build(settings)`, which now returns the failover chain — so a 429 sends the eval to
      `claude_cli` and it reports *haiku's* precision and recall as though they were the
      free model's, at $0 charged, with nothing on screen looking wrong. Run it with
      `MODEL_FALLBACK_BACKEND=` unset, or print `fallback_stats(client)` at the end and
      throw the run away if anything crossed over. The shootout is unaffected — it calls
      the endpoint directly. Second trap: the two tools together spend ~24 calls of a
      50/day quota, i.e. they consume the budget they are measuring. Run them first, not
      after a sync.

## Third: free wins, no model involved

**45 mail senders have ≥5 items each and have never once produced a keep**, and are not in
`learned_noise` (117 entries, all promoted 2026-08-13 and never refreshed since). They have
consumed 881 model triage calls all-time, and are still arriving at **5.5/day against a
mail rate of 39.7/day — 14% of all mail removed before any model call**, permanently, at
zero risk.

Top offenders: `noreply@medium.com` (141), `notifications@vercel.com` (78),
`notifications@github.com` (47+21), `notifications@email.credible.com` (47),
`notifications-noreply@linkedin.com` (29), `noreply@email.act.org` (25).

The machinery already exists — `backglass noise` promotes senders the triage model keeps
dropping. It simply has not been run in eleven days.

- [ ] **T4. Run `backglass noise`, review, promote.** Two cautions, both from this data:
      `notifications@instructure.com` (35) is Canvas and `sundevilparents@reply.asu.edu`
      (25) is *already* in `NOISE_SENDERS` yet still shows triage verdicts — check whether
      tier-0 matching is working before trusting the list.
- [ ] **T5. Make the refresh recurring rather than a thing someone remembers.** Eleven days
      of drift produced 881 wasted calls; the next eleven will too. Either a monthly
      routine or a line on the run report when N candidates are waiting.

## Fourth: the quality problem, which is not a cost problem

Triage judges **one item with no conversation context** — `From/Date/Subject` plus a body
excerpt, and nothing else (`triage.py`, `BODY_LIMIT = 2000`). For mail that is fine. For
chat it is close to undecidable, and the ledger shows the model straining against it:

```
'Yes'                    -> "Decision/confirmation: user responds 'Yes' confirming something discussed"
'Ok'                     -> "'Ok' is a short reply confirming/accepting something."
'Good'                   -> "One-word reply in a personal thread; plausibly c…"
'Ohh yea 64'             -> "user confirms 'Ohh yea 64' choosing water bottle size"
```

That last one is the tell. Nothing in a 10-character message says "water bottle size", so
that phrase came from somewhere outside the item — most plausibly the *sibling messages
sharing the batch prompt*, since `owner_context` carries commitments, plan and questions
rather than chat threads. Stated as the inference it is: `source_item` does not record
whether a verdict came from a batch or a per-item call, so this is not yet proven, and
T1/T2's instrumentation is what would settle it. The proposal below stands either way,
because the design question — *should* chat triage see its thread — does not depend on
whether the batch is currently leaking one by accident.

Which produces a genuinely backwards property: **an item escalated out of the batch is
re-read with less information than the batch had.** Per-item gives 2000 characters of one
message instead of 500; for a long email that is more context, for a chat message it is the
same three words with the thread stripped away. The careful pass is the blind one, exactly
for the items — short chat replies — where context is the whole question.

- [ ] **T6. Give chat triage its thread.** For `imessage`/`instagram`/`slack` items, include
      the preceding few messages of the same conversation as context, marked as context and
      not as the item under judgement. Make the accidental thing deliberate, and make the
      escalation path strictly more informed than the batch rather than differently
      informed. Measure against `evals/golden_triage.json` before and after — and extend
      that set, which is 10 mail-shaped cases and contains no chat at all.

      **docs/08 holds, and it is worth saying because this ships more private text per
      call.** Thread context is drawn from stored `source_item` rows, and anything the
      boundary excluded never became one — an excluded message is "not stored, not hashed,
      counted only as a tally". So the context window can only ever contain text the
      boundary already cleared. It does mean more of that text per call, which is an
      argument for the context being short and for the provider being one that does not
      train on inputs (see `tasks/free-inference-2026-08-24.md`).
- [ ] **T7. Add chat cases to the golden set.** A triage eval with no short-reply cases
      cannot see the failure mode this section is about.

## Rejected, with the evidence, so nobody builds it later

**A length pre-filter for chat.** The obvious idea — drop very short messages for free
before the model — is disqualified:

| cut | items caught free | of which real keeps | false-negative rate |
|---|---|---|---|
| ≤15 chars | 2,289 (49% of chat triage) | 258 | 11.3% |
| ≤25 chars | 3,311 (71%) | 433 | 13.1% |
| ≤40 chars | 4,042 (86%) | 559 | 13.8% |

The saving is real and so is the cost: at ≤25 characters it would silently discard 433
commitments. And the specific casualties are the point — `"Im volunteering"`,
`"I am free in 30min"`, `"I am going home at 5"`, `"Yes"`, `"Sure"`. Short is precisely
where a chat commitment lives, because the commitment is the *reply*. Length is
anti-correlated with what this rule would need it to mean.

`MODEL_FALLBACKS`-style model rotation is also not a triage lever: measured today, the
OpenRouter daily cap is account-wide, so rotating models buys nothing once the day is
spent.

## Order, and why

**T4 first** — free, zero-risk, permanent, and it shrinks the population everything else
operates on. It is also the only item here that needs no measurement to justify: 45 senders
with zero keeps across 881 items is not a judgement call.

**T3 next, at the quota reset**, because every quality number in this document is
contaminated by a 69% call-failure rate, and both T1 and T6 need a clean baseline to be
measured against.

**T1 after a few clean syncs** — and quite possibly that closes it. The escalation gap
looks like a provider symptom, and the provider was fixed today. T2 only if T1 says the
ratio is still high.

**T6/T7 last.** The largest change, the only one that alters what the model is asked, and
the only one that increases how much text leaves the machine per call. It should land
against a stable baseline rather than into the current noise.

The through-line: three of the seven items are measurements rather than changes, because
the two things that looked most broken today — escalation and triage quality — were both
being measured through a failing provider. Fix the instrument, then read it.

**Not proposed:** tuning the "when in doubt, keep" instruction. `triage.md` says not to
trade recall for cost, the spend cap is the cost mechanism, and the failover chain built
today already removed the pressure that would tempt it.
