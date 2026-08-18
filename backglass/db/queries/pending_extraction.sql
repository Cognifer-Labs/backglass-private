-- Items that survived triage and have not been extracted at the current prompt version.
--
-- Bumping the prompt version makes every kept item pending again — the re-extraction
-- path docs/02 §Immutable source items describes — UNLESS the new version's frontmatter
-- names the old stamp in `compatible:`, which says the change is additive and the old
-- rows stay done. Nothing is ever re-fetched either way.
--
-- Params: :user_id, :compatible_versions (comma-joined stamps)
SELECT id, source, external_id, occurred_at, author, title, body_text, raw_json
FROM source_item
WHERE user_id = :user_id
  AND triage_verdict = 'keep'
  -- :compatible_versions is every stamp that counts as done — the current one plus the
  -- prompt's `compatible:` list — comma-joined by the caller. instr() with commas on
  -- both sides so `@9` can never match inside `@19`.
  AND (extraction_version IS NULL
       OR instr(',' || :compatible_versions || ',', ',' || extraction_version || ',') = 0)
-- Oldest first, and this is not cosmetic. Post-processing steps 4 and 5 are order
-- dependent: a message that resolves an earlier commitment can only supersede a row that
-- already exists. Extract newest-first and a first-run backfill processes "here's that
-- plan I promised" before the promise itself, so supersession never fires, and the
-- original promise is then silently deduped away against the resolution. The ledger has
-- to be built in the order the events actually happened.
ORDER BY datetime(occurred_at) ASC;
