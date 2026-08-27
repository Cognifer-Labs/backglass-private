-- The evolving state doc, stored as versions of a rendering.
--
-- The owner, 2026-08-20: "create an evolving state doc about me". This table is the
-- *evolution*, not the state: the state itself is the `fact` table, which already has
-- supersession, citations, status, confidence and a page the owner curates it on.
-- "Enrolled at ASU Tempe" is fact row 5 and it must stay fact row 5 — a second table
-- holding owner-state would drift from the first within a month, and CLAUDE.md's
-- ledger-stays-primary rule is what forbids it (tasks/todo.md "Goal 3", stated
-- assumption 2; tasks/pipeline-redesign-2026-08-21.md §3.4).
--
-- So `situation_doc` stores a rendered body and its hash, and nothing else. Nothing in
-- the pipeline may read the stored body to decide anything: `backglass/situation.py`
-- renders fresh from the ledger for every reader, and the stored rows exist so the
-- document has a *history*. A new row is written only when the hash changes, which is
-- rule 3 for this table and also the only form in which the history is readable — a
-- version per sync would be a log of how often the job ran, not a record of what moved.
--
-- The diff between two versions is the evolution the owner asked to see, and it costs
-- nothing to maintain: facts supersede, the next rendering differs, the change is legible.
--
-- No `user_id` UNIQUE and no dedup key: the whole point is many rows over time. The
-- hash gate lives in `situation.save()` rather than in a constraint, because "the same
-- body as the *previous* version" is not "the same body as any version" — a fact that
-- flips back to a value it held in June is a real change and gets its own row.
--
-- Sidecar: this migration re-arms the frozen-app crash (docs/13). Rebuild the desktop
-- sidecar after applying, or say plainly that it is behind.

CREATE TABLE situation_doc (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL DEFAULT 1,
  body       TEXT    NOT NULL,
  body_hash  TEXT    NOT NULL,   -- sha256 of body; the gate that makes a write mean something
  created_at TEXT    NOT NULL
);

CREATE INDEX idx_situation_doc_recent ON situation_doc(user_id, id DESC);
