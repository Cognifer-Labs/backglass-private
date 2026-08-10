-- Retrieval over the document pile. Owner's ruling, 2026-08-10.
--
-- docs/02 §Why no vector database is revised rather than deleted: its five queries are
-- still WHERE clauses over typed columns and stay that way. What it did not anticipate is
-- the drop folder — a signed contract, a scanned letter, meeting notes — where "which
-- letter mentioned the deposit deadline" is not a column and never becomes one.
--
-- Additive by construction. Nothing in the brief, planner or dashboard reads a row here;
-- delete the table and every existing surface is still correct. That is what keeps the
-- ledger primary and stops this becoming the search-over-a-pile the product rejects.
--
-- One row per (source_item, model). The model is part of the identity because vectors
-- from two models are not comparable: swapping the embedding model must re-index rather
-- than silently rank against a mixed space, which is the failure that looks like bad
-- results and is actually a bug.
--
-- The vector is a BLOB of little-endian float32, `dim` floats long. No sqlite-vec, no
-- faiss, no numpy requirement: a personal ledger's document count is thousands, and a
-- cosine over a few thousand vectors in pure Python is milliseconds. When it stops being
-- milliseconds, that is the moment to add an index — not before, and the shape here does
-- not prevent it.
CREATE TABLE embedding (
  id             INTEGER PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  source_item_id INTEGER NOT NULL REFERENCES source_item(id) ON DELETE CASCADE,
  model          TEXT    NOT NULL,
  dim            INTEGER NOT NULL,
  vector         BLOB    NOT NULL,
  -- What was embedded, so a later reader can tell a title-only vector from a full-text
  -- one without re-deriving it, and so re-indexing can skip unchanged text.
  text_hash      TEXT    NOT NULL,
  chars          INTEGER NOT NULL,
  created_at     TEXT    NOT NULL,
  UNIQUE (user_id, source_item_id, model)
);

CREATE INDEX idx_embedding_model ON embedding(user_id, model);
