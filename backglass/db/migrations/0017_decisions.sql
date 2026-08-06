-- Major decisions: choices the owner has settled, recorded once so nothing keeps
-- asking. "Sallie Mae application — not doing it" is neither an open commitment
-- (nothing left to do) nor a done one (nothing was done); it is a claim with a
-- lifecycle, the fact pattern plus one link into the commitment ledger. Recording a
-- decision may close the open commitment it settles; the link preserves which one.
-- Same rules as fact: supersession over UPDATE, retraction over DELETE.
CREATE TABLE decision (
  id             INTEGER PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  title          TEXT    NOT NULL,          -- what was decided about, owner's words
  choice         TEXT    NOT NULL,          -- what was decided
  reasoning      TEXT,                      -- why, optional
  commitment_id  INTEGER REFERENCES commitment(id),
  status         TEXT    NOT NULL DEFAULT 'active', -- active|superseded|retracted
  superseded_by  INTEGER REFERENCES decision(id),
  decided_at     TEXT    NOT NULL,
  created_at     TEXT    NOT NULL
);

CREATE INDEX idx_decision_active ON decision(user_id, status) WHERE status = 'active';
