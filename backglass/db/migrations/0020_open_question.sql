-- Things Backglass cannot settle from the evidence, asked instead of guessed.
--
-- CLAUDE.md rule 2 keeps a low-confidence extraction out of the brief, and the review
-- queue asks about it. But a confusion is not always about one record's confidence.
-- Two classes scheduled at the same hour are each perfectly confident; the problem is
-- that both cannot be true. An untitled event is not a guess about a commitment, it is
-- an hour of the day nobody can account for. "Nyasha" and "Mrs. Shepard" are two
-- confident entities that are probably one person. None of those fit the accept/reject
-- shape, and all of them silently degrade the plan while nothing anywhere says so.
--
-- Identity is (kind, subject_key), so a detector re-running finds its own prior question
-- rather than asking again every sync — the monitored_chat rule, which the dedup queue
-- also follows. `subject_key` is whatever makes the question unique to its detector: a
-- sorted pair of event ids, an entity pair, a date and a title.
--
-- An answer is never an UPDATE to the thing it was about. It is recorded here, and where
-- it settles something durable it also goes to `decisions` or `fact` with provenance
-- pointing back. That keeps this table a record of questions asked and answered, not a
-- second source of truth about the ledger.
--
-- `answer_text` is free text and is the point: the options a detector can enumerate are
-- the cases it thought of, and the owner routinely knows a fifth one. A question answered
-- in the owner's own words is worth more than one answered by the closest available
-- button, so the box is always there and never secondary to the buttons.
CREATE TABLE open_question (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL DEFAULT 1,
  kind          TEXT    NOT NULL,   -- conflict|untitled|duplicate_entity|contradiction|priority
  subject_key   TEXT    NOT NULL,   -- unique within kind; how a detector finds its own row
  question      TEXT    NOT NULL,   -- the sentence the owner reads
  detail        TEXT,               -- the evidence, rendered; never the only place it lives
  options_json  TEXT    NOT NULL DEFAULT '[]',
  status        TEXT    NOT NULL DEFAULT 'open',  -- open|answered|dismissed
  answer_option TEXT,               -- the option chosen, when one was
  answer_text   TEXT,               -- the owner's own words, always allowed
  asked_at      TEXT    NOT NULL,
  answered_at   TEXT,
  UNIQUE (user_id, kind, subject_key)
);

CREATE INDEX idx_open_question_open ON open_question(user_id, status) WHERE status = 'open';
