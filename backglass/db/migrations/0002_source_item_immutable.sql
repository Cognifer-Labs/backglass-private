-- docs/03-data-model.md: "Raw items are written once and never modified. Extraction
-- reads them and writes derived records elsewhere." The three columns extraction owns
-- are the documented exceptions.
--
-- This is a trigger rather than a code convention for the same reason boundary.py lives
-- in the connector package: bypassing it has to be a visible edit. A re-extraction that
-- quietly rewrote body_text would destroy the one property that makes re-running
-- extraction against a better prompt safe.
--
-- `IS NOT` rather than `!=` because SQLite's `!=` is NULL-propagating and half these
-- columns are nullable.

CREATE TRIGGER source_item_immutable
BEFORE UPDATE ON source_item
FOR EACH ROW
WHEN OLD.user_id      IS NOT NEW.user_id
  OR OLD.source       IS NOT NEW.source
  OR OLD.external_id  IS NOT NEW.external_id
  OR OLD.fetched_at   IS NOT NEW.fetched_at
  OR OLD.occurred_at  IS NOT NEW.occurred_at
  OR OLD.author       IS NOT NEW.author
  OR OLD.title        IS NOT NEW.title
  OR OLD.body_text    IS NOT NEW.body_text
  OR OLD.raw_json     IS NOT NEW.raw_json
  OR OLD.content_hash IS NOT NEW.content_hash
BEGIN
  SELECT RAISE(
    ABORT,
    'source_item is immutable; only triage_verdict, triage_reason and extraction_version may change (docs/03)'
  );
END;
