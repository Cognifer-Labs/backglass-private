-- Items that survived triage and have not been extracted at the current prompt version.
--
-- Bumping the prompt version makes every kept item pending again, which is the
-- re-extraction path docs/02 §Immutable source items describes. Nothing is re-fetched.
--
-- Params: :user_id, :extraction_version
SELECT id, source, external_id, occurred_at, author, title, body_text, raw_json
FROM source_item
WHERE user_id = :user_id
  AND triage_verdict = 'keep'
  AND (extraction_version IS NULL OR extraction_version != :extraction_version)
-- Oldest first, and this is not cosmetic. Post-processing steps 4 and 5 are order
-- dependent: a message that resolves an earlier commitment can only supersede a row that
-- already exists. Extract newest-first and a first-run backfill processes "here's that
-- plan I promised" before the promise itself, so supersession never fires, and the
-- original promise is then silently deduped away against the resolution. The ledger has
-- to be built in the order the events actually happened.
ORDER BY datetime(occurred_at) ASC;
