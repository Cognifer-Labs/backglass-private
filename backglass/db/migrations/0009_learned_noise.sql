-- Senders the tier-1 model kept dropping, promoted into free tier-0 drops.
--
-- Promotion is evidence-gated in code (backglass/extract/noise.py): never a sender
-- with any kept item, ever — a false negative loses a commitment permanently, so the
-- bar is absolute, not statistical. Rows are disabled, never deleted: `enabled = 0`
-- is the undo path and the audit trail at once.

CREATE TABLE learned_noise (
  id             INTEGER PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  kind           TEXT    NOT NULL CHECK (kind IN ('address', 'domain')),
  value          TEXT    NOT NULL,          -- lowercased bare address or domain
  evidence_count INTEGER NOT NULL,          -- model-drop verdicts at promotion time
  first_seen     TEXT,                      -- occurred_at of the earliest evidence item
  last_seen      TEXT,
  promoted_at    TEXT    NOT NULL,
  promoted_by    TEXT    NOT NULL DEFAULT 'cli',  -- 'cli' | 'auto'
  enabled        INTEGER NOT NULL DEFAULT 1,
  UNIQUE (user_id, kind, value)
);
