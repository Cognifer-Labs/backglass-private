-- What the logic checker has already judged, so it judges each obligation once.
--
-- The deterministic half of the checker (backglass/logic.py) needs no memory: every rule
-- reads a state its own disposal ends, so a second pass finds nothing. The model-judged
-- half does. "Does this still make sense, given what the ledger records about the owner?"
-- costs a call, and without a record of the answer every sync would re-send two hundred
-- open commitments and re-decide them — money for nothing, and a `keep` verdict that
-- flickers to `nonsense` on a later pass would be a row disappearing for no reason the
-- owner can see.
--
-- One row per commitment, ever (UNIQUE on commitment_id). A `keep` is as worth storing as
-- a `nonsense`: it is the thing that stops the pass asking again.
--
-- `fact_id` is the citation, and it is what separates this from a guess. A `nonsense`
-- verdict must name the recorded fact it contradicts — "enrolled at ASU Tempe" against a
-- UT Dallas scholarship deadline — and that id is intersected with the fact table before
-- anything is dropped. `quote` is the words from the source item that identify what is
-- being judged, verified against the item like recheck's quotes are. A verdict that
-- cannot cite both is discarded, not repaired.
--
-- `status`: applied (acted on), pending (below the drop threshold, waiting on the owner
-- through a question), kept (the model says it still makes sense).
CREATE TABLE logic_check (
  id             INTEGER PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  commitment_id  INTEGER NOT NULL REFERENCES commitment(id),
  verdict        TEXT    NOT NULL,           -- keep|nonsense
  confidence     REAL    NOT NULL,
  fact_id        INTEGER REFERENCES fact(id),
  quote          TEXT,
  reason         TEXT,
  status         TEXT    NOT NULL DEFAULT 'pending', -- applied|pending|kept
  created_at     TEXT    NOT NULL,
  decided_at     TEXT,
  UNIQUE (user_id, commitment_id)
);

CREATE INDEX idx_logic_check_pending ON logic_check(user_id, status) WHERE status = 'pending';
