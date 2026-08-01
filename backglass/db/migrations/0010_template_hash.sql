-- Derived, content-addressed like content_hash: sha256 over (sender domain, title
-- skeleton, body skeleton) with URLs, dates, and digits stripped — the shape of a
-- templated mail with its variable parts removed (backglass/extract/templates.py).
--
-- Computed at ingest for new rows; existing rows are backfilled by
-- `backglass noise templates --backfill` (the skeleton logic is Python, and
-- migrations are checksummed-immutable pure SQL). Deliberately NOT added to the 0002
-- immutability trigger's protected list: the hash is recomputable, never authored.

ALTER TABLE source_item ADD COLUMN template_hash TEXT;

CREATE INDEX idx_source_template ON source_item(user_id, template_hash)
  WHERE template_hash IS NOT NULL;
