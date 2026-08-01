-- Outstanding Message Batches and their item mapping. docs/02 §Cost control: batch
-- mode trades latency for a 50% discount on extraction (backglass/batch.py).
--
-- A child table rather than an items_json blob — every query stays a SELECT you can
-- read aloud, and per-item status feeds the costs command and doctor.

CREATE TABLE model_batch (
  id           INTEGER PRIMARY KEY,
  user_id      INTEGER NOT NULL DEFAULT 1,
  batch_id     TEXT    NOT NULL UNIQUE,          -- msgbatch_...
  kind         TEXT    NOT NULL DEFAULT 'extract',
  model        TEXT    NOT NULL,                  -- concrete model id at submit time
  prompt_stamp TEXT    NOT NULL,                  -- extraction_version this batch targets
  status       TEXT    NOT NULL DEFAULT 'submitted',
               -- submitted | collected | expired | failed | canceled
  created_at   TEXT    NOT NULL,
  collected_at TEXT,
  spend_cents  INTEGER NOT NULL DEFAULT 0,
  error        TEXT
);

CREATE TABLE model_batch_item (
  id             INTEGER PRIMARY KEY,
  batch_id       TEXT    NOT NULL REFERENCES model_batch(batch_id),
  custom_id      TEXT    NOT NULL,                -- "si-<source_item_id>"
  source_item_id INTEGER NOT NULL REFERENCES source_item(id),
  status         TEXT    NOT NULL DEFAULT 'pending',
                 -- pending | succeeded | errored | expired
  UNIQUE (batch_id, custom_id)
);

CREATE INDEX idx_batch_item_source ON model_batch_item(source_item_id);
