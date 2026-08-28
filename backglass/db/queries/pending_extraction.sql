-- Items that survived triage and have not been extracted at the current prompt version.
--
-- Bumping the prompt version makes every kept item pending again — the re-extraction
-- path docs/02 §Immutable source items describes — UNLESS the new version's frontmatter
-- names the old stamp in `compatible:`, which says the change is additive and the old
-- rows stay done. Nothing is ever re-fetched either way.
--
-- A manual item is never pending, and the reason it looked pending is that its stamp is
-- not a prompt version. `web/actions.py::quick_add` writes 'manual' into
-- `extraction_version` and inserts the commitment the owner typed in the same breath;
-- 'manual' can never appear in :compatible_versions, so the row came back pending on
-- every prompt bump and the model wrote another commitment beside the owner's own.
-- Measured 2026-08-27: 37 manual items, none still stamped 'manual', carrying 81
-- commitments where 34 were typed — 39 of the surplus open. See
-- pending_extraction_unbatched.sql for the full note.
--
-- Params: :user_id, :compatible_versions (comma-joined stamps)
SELECT id, source, external_id, occurred_at, author, title, body_text, raw_json
FROM source_item
WHERE user_id = :user_id
  AND triage_verdict = 'keep'
  AND source <> 'manual'
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
