-- One embedding store, not one per thing embedded.
--
-- 0021 keyed vectors to `source_item` because documents were the only thing being
-- retrieved. Measuring the ledger's redundancy showed the same vectors answer a second
-- question: of 191 open commitments, the token-set matcher calls 39 pairs duplicates and
-- an embedding calls 46, with 13 that only meaning catches — "Tell Mrs. Gathas you are
-- back in Arizona" against "…he's back in Arizona" scores 0.81 lexically and 0.94
-- semantically, and they are one promise.
--
-- Adding a `commitment_embedding` table beside the first would have been the same six
-- columns twice, which is the redundancy this change exists to reduce. So the row names
-- what it points at: `kind` plus `ref_id`, and a new kind is a string rather than a table.
--
-- The FK to source_item goes with it. A `ref_id` cannot reference two parents, so
-- referential integrity moves to the writers and to the prune paths that already
-- enumerate their dependents — the cost of one store, paid knowingly. `embedding` is a
-- derived cache: worst case a stale row ranks nothing, because every read joins back to
-- the live table and a vanished parent drops out of the join.
--
-- Recreated rather than altered because SQLite cannot drop a constraint, and because
-- 0021 shipped hours ago: no installation has vectors worth migrating, and re-indexing
-- is a minute of local compute (1,247 documents in 65 seconds).
DROP TABLE IF EXISTS embedding;

CREATE TABLE embedding (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL DEFAULT 1,
  -- source_item | commitment. Part of the identity, so one ref_id may be embedded once
  -- per kind without collision.
  kind       TEXT    NOT NULL,
  ref_id     INTEGER NOT NULL,
  model      TEXT    NOT NULL,
  dim        INTEGER NOT NULL,
  vector     BLOB    NOT NULL,
  text_hash  TEXT    NOT NULL,
  chars      INTEGER NOT NULL,
  created_at TEXT    NOT NULL,
  UNIQUE (user_id, kind, ref_id, model)
);

CREATE INDEX idx_embedding_lookup ON embedding(user_id, kind, model);
