-- Two plans the owner has said are genuinely two plans.
--
-- The exact shape of `commitment_distinct` (0018), for the exact reason, one table over:
-- an unremembered "no" re-surfaces every morning forever. What is new is that engagements
-- needed it at all, and why they did.
--
-- `extract/engagements._same_row` is deliberately conservative. Its docstring records four
-- rounds of verification behind one sentence: "a duplicate is visible on the board and can
-- be dismissed, while a wrongly merged plan silently replaces one the owner had already
-- agreed to." That trade-off is correct and this migration does not touch it.
--
-- What was never built is the half the trade-off leans on. Measured on the owner's ledger
-- on 2026-08-24: 127 same-day near-duplicate plans, 120 of them pairs whose wording scores
-- below the 0.85 the matcher requires — the matcher working exactly as designed. And there
-- was nowhere to dismiss a single one of them. The cost was being paid and the compensation
-- did not exist.
--
-- Pair normalized low<high, so (a,b) and (b,a) are one row and the UNIQUE actually holds.
-- Both columns are real foreign keys: a pair naming a plan that does not exist is a bug in
-- the writer, and unlike `claim_dependency`'s deliberately unenforced pointer there is
-- exactly one table these can point at.

CREATE TABLE engagement_distinct (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL DEFAULT 1,
  low_id     INTEGER NOT NULL REFERENCES engagement(id),
  high_id    INTEGER NOT NULL REFERENCES engagement(id),
  decided_at TEXT    NOT NULL,
  UNIQUE (user_id, low_id, high_id)
);
