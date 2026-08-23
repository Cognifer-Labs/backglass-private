-- The change ledger. tasks/pipeline-redesign-2026-08-21.md §2.3/§3.3: one append-only
-- record of every belief-layer status change, subsuming four mechanisms that had each
-- grown their own local answer to "what changed since I last looked?" —
-- `day_plan.inputs_fingerprint` (0028, hash-the-world), `logic_check` judged-once (0029,
-- no way to notice a fact moved), goal 3's planned `commitment_dependency` (never built —
-- see tasks/todo.md "Goal 3"), and the immutable-conflict spam from a re-read the ledger
-- has no vocabulary for (tasks/audit-2026-08-21.md §2).
--
-- `claim_event` is the ledger; `claim_dependency` is what goal 3 called
-- `commitment_dependency`, generalized past commitment to any subject table — the same
-- three stated assumptions from that design carry over unchanged: deleted means
-- tombstoned, `none` is a first-class dependency (not every claim depends on something),
-- and this is additive machinery beside the typed tables, never a second store of the
-- facts themselves.
--
-- `subject_table`/`subject_id` is a deliberately unenforced pointer, not a foreign key —
-- SQLite cannot express "references whichever table this row names," and open_question's
-- own `subject_key` already carries the same shape in this schema. Callers are
-- responsible for the pointer being valid; nothing here can check it, so treat a bad
-- pointer as a bug in the writer, not a constraint the database will catch.
--
-- This migration adds the tables and their read/write module (backglass/claim_events.py).
-- Wiring is partial by design, landed incrementally: facts.py (supersede + retract) and
-- web/actions.py (resolve + drop) emit events in this change; logic.py's dispositions and
-- the assignment/retraction mirrors are the next wiring pass, not this one. An event that
-- is never emitted for some writer means only that writer's changes are invisible to
-- anything reading the ledger — the same "additive, never load-bearing" boundary
-- CLAUDE.md draws for retrieval applies here: nothing in the pipeline may depend on the
-- ledger being complete until every writer is wired.

CREATE TABLE claim_event (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL DEFAULT 1,
  at            TEXT    NOT NULL,
  subject_table TEXT    NOT NULL,   -- 'commitment' | 'fact' | 'open_question' | ...
  subject_id    INTEGER NOT NULL,
  field         TEXT,               -- the column that changed, or NULL for a whole-row event
  old_value     TEXT,
  new_value     TEXT,
  cause         TEXT    NOT NULL    -- free text: 'fact_superseded', 'logic:reported-done', ...
);

CREATE INDEX idx_claim_event_subject ON claim_event(user_id, subject_table, subject_id, id);
CREATE INDEX idx_claim_event_recent ON claim_event(user_id, at);

-- One row per (subject, dep_key): what a claim's current standing rests on. `dep_key` is
-- a text discriminator ('fact:5', 'none', 'commitment:42') rather than a UNIQUE across the
-- nullable columns directly, for the reason goal 3's design already states — SQLite treats
-- NULLs as distinct, so a NULL-bearing UNIQUE enforces nothing (this table's own
-- `fact_id` would otherwise be exactly that hole).
CREATE TABLE claim_dependency (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL DEFAULT 1,
  subject_table TEXT    NOT NULL,
  subject_id    INTEGER NOT NULL,
  dep_key       TEXT    NOT NULL,
  kind          TEXT    NOT NULL,             -- 'fact' | 'none'
  fact_id       INTEGER REFERENCES fact(id),
  quote         TEXT,
  reason        TEXT    NOT NULL,
  status        TEXT    NOT NULL DEFAULT 'active',  -- active|superseded
  superseded_by INTEGER REFERENCES claim_event(id),
  created_at    TEXT    NOT NULL,
  UNIQUE (user_id, subject_table, subject_id, dep_key)
);

CREATE INDEX idx_claim_dependency_fact
  ON claim_dependency(user_id, fact_id) WHERE status = 'active';
