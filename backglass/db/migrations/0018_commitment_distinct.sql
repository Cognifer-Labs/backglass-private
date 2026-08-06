-- The owner's answer to "are these two the same promise?", remembered.
--
-- The 0.85 auto-dedup joins restatements it is sure about; pairs below it that still
-- read alike are a question, and a question the system asks must never be asked twice
-- (the monitored_chat rule). "Same thing" is recorded on the commitment itself
-- (superseded_by); "different" needs its own row or the pair is re-suggested forever.
-- Pairs are stored normalized (low_id < high_id) so one decision covers both orders.
CREATE TABLE commitment_distinct (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL DEFAULT 1,
  low_id     INTEGER NOT NULL REFERENCES commitment(id),
  high_id    INTEGER NOT NULL REFERENCES commitment(id),
  decided_at TEXT    NOT NULL,
  UNIQUE (user_id, low_id, high_id)
);
