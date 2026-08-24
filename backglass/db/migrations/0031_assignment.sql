-- Coursework as a typed record, beside the immutable item that first reported it.
--
-- 159 Canvas assignments were in the ledger on 2026-08-20 and 120 of the open ones
-- carried the same `type_default:30`. "Take PSY101 Exam 4 (Ch. 13-15) via LockDown
-- Browser" and "Complete LearningCurve 14a" were the same half hour, so every capacity
-- number the planner produced over coursework was arithmetic over one constant.
--
-- The feed already carries what answers it. 74 of the 169 assignments in the ASU ICS
-- document have a real DESCRIPTION — instructions, tool names, links, page counts — and
-- many state their own length in the title (`1-1-1 - Tech in the 21st Century (12:35)`).
-- `canvas_ics` stored `"<course>: <title> is due <due>."` and dropped the rest.
--
-- Why this is a table and not a wider `source_item.body_text`, stated here because it is
-- the first thing anyone will try: `ledger.upsert_source_item` treats a differing
-- `content_hash` on a stored `external_id` as a conflict — it records it and skips,
-- because 0002 makes the item immutable. Widening the body therefore rewrites the hash
-- of all 159 stored rows, emits 159 `content changed for an immutable source_item`
-- errors, exits non-zero, and delivers no new data at all. The hash covers (author,
-- title, body_text, occurred_at) and not `raw_json`, so the raw record can be widened
-- for future items at no cost, but the backlog needs a home of its own.
--
-- And an assignment is not the sentence that first reported it. It is a live upstream
-- record: due dates move, instructions get edited. Five CIS236 assignments had already
-- moved when this was written — 1-1-1 through 1-1-4 from 08-23 to 08-25, the Team
-- Charter from 08-31 to 09-04 — and the ledger still held the old dates, because the
-- re-read produced a conflict and a conflict propagates nothing. `last_seen_at` and
-- `description_hash` are what make that change visible instead of silent.
--
-- Additive, per CLAUDE.md: the obligation still lives in `commitment`, the evidence still
-- lives in `source_item`, and if this table vanished every surface would still be
-- correct — back to a flat 30 minutes, which is where it started.
CREATE TABLE assignment (
  id               INTEGER PRIMARY KEY,
  user_id          INTEGER NOT NULL DEFAULT 1,
  source           TEXT    NOT NULL,              -- canvas:ics
  external_id      TEXT    NOT NULL,              -- assignment:7833000, the item's own id
  source_item_id   INTEGER REFERENCES source_item(id),
  course           TEXT    NOT NULL DEFAULT '',
  title            TEXT    NOT NULL,
  due_at           TEXT,
  url              TEXT    NOT NULL DEFAULT '',
  description      TEXT    NOT NULL DEFAULT '',
  -- What "changed" means for this row. Not the fetch time: a feed re-read that returns
  -- the same document must write nothing (rule 3), and a description edit must re-open
  -- the effort question that was answered from the old text.
  description_hash TEXT    NOT NULL,
  effort_minutes   INTEGER,
  effort_basis     TEXT,                          -- stated_video|stated_words|…|type:<k>
  effort_quote     TEXT,                          -- the words the number was read from
  sessions         INTEGER NOT NULL DEFAULT 1,
  analyzed_hash    TEXT,                          -- the description_hash it was read from
  first_seen_at    TEXT    NOT NULL,
  -- Last *changed*, not last seen. A feed re-read that returns the same document must
  -- write nothing (rule 3), and a column bumped on every fetch would make every sync a
  -- write for all 159 rows — so the honest name is the one that matches the behaviour.
  -- "Did the feed still list it today" is `retraction`'s question and it has its own
  -- table; this one answers "when did this assignment last move".
  last_changed_at  TEXT    NOT NULL,
  UNIQUE (user_id, source, external_id)
);

CREATE INDEX idx_assignment_due ON assignment(user_id, due_at);

CREATE INDEX idx_assignment_item ON assignment(source_item_id);

-- What the owner needs in front of them before the work can start: a chapter range, a
-- browser the exam refuses to run without, a dataset, the milestone this one builds on.
--
-- `quote` is not decoration. Rule 1 applies inside this table: a material with no
-- evidence is a guess, and a guess the owner has to go and check by hand is the work
-- this is supposed to remove.
--
-- Every column in the UNIQUE is NOT NULL on purpose. SQLite counts NULLs as distinct, so
-- a NULL-bearing UNIQUE enforces nothing and the second pass would duplicate every row
-- it wrote on the first. That constraint is rule 3 for this table, so it has to hold.
CREATE TABLE assignment_material (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL DEFAULT 1,
  assignment_id INTEGER NOT NULL REFERENCES assignment(id) ON DELETE CASCADE,
  kind          TEXT    NOT NULL,                 -- reading|software|link|document|other
  name          TEXT    NOT NULL,
  detail        TEXT    NOT NULL DEFAULT '',
  quote         TEXT    NOT NULL DEFAULT '',
  basis         TEXT    NOT NULL DEFAULT 'deterministic',  -- deterministic|model
  created_at    TEXT    NOT NULL,
  UNIQUE (user_id, assignment_id, kind, name)
);

CREATE INDEX idx_assignment_material ON assignment_material(user_id, assignment_id);
