-- Personal knowledge base (Phase 12). A fact is a small claim with provenance and a
-- lifecycle — the commitment pattern minus a due date. Writing the same (subject, key)
-- again supersedes the active row; nothing is ever deleted, so memory can change its
-- mind without losing what it used to believe (rule 4, applied to memory).
CREATE TABLE fact (
  id             INTEGER PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  subject        TEXT    NOT NULL,          -- kebab lane: identity|housing|premed|...
  key            TEXT    NOT NULL,
  value          TEXT    NOT NULL,
  note           TEXT,                      -- evidence pointer, in words
  source         TEXT    NOT NULL,          -- manual|extraction|assistant
  source_item_id INTEGER REFERENCES source_item(id),
  status         TEXT    NOT NULL DEFAULT 'active', -- active|superseded|retracted
  superseded_by  INTEGER REFERENCES fact(id),
  created_at     TEXT    NOT NULL
);

CREATE INDEX idx_fact_active ON fact(user_id, subject, key) WHERE status = 'active';
