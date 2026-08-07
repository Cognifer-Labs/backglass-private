-- Phase 0 of the backend plan: one row per model call, so the next decision about
-- this pipeline is measured rather than argued.
--
-- Until now the only telemetry was run.spend_cents, a single per-run total that mixes
-- triage and extraction together. That number cannot answer any of the questions the
-- plan actually turns on: what a call costs against its payload size, how much of a
-- 90-second sync is spent waiting on which tier, or whether per-call session overhead
-- dominates on the CLI backend. The 2026-07-30 lesson measured that overhead once, in a
-- different context, and every cost argument since has been reasoning from it.
--
-- `run_id` is nullable and stamped when the run row is written at the end of the pass —
-- the run does not exist while its calls are being made. A row whose run never finished
-- (a crash, a kill) keeps run_id NULL and is still the truth about a call that happened.
--
-- `cost_usd` is stored as REAL and may be imputed: on subscription auth the CLI reports
-- what the call would have cost on the API. `spend_is_imputed` decides whether that may
-- stop work; it has never decided whether it is worth recording, and it is.
CREATE TABLE model_call (
  id           INTEGER PRIMARY KEY,
  user_id      INTEGER NOT NULL DEFAULT 1,
  run_id       INTEGER REFERENCES run(id),
  tier         TEXT    NOT NULL,          -- triage | triage_batch | extract
  model        TEXT    NOT NULL,
  prompt_chars INTEGER NOT NULL DEFAULT 0,
  duration_ms  INTEGER NOT NULL DEFAULT 0,
  cost_usd     REAL    NOT NULL DEFAULT 0,
  outcome      TEXT    NOT NULL,          -- ok | error | rate_limited | auth
  started_at   TEXT    NOT NULL
);

CREATE INDEX idx_model_call_run ON model_call (user_id, run_id);
CREATE INDEX idx_model_call_tier ON model_call (user_id, tier, started_at);
