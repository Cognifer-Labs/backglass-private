-- Surviving senders by extraction volume this month — where the tier-2 money goes.
--
-- Attribution caveat, stated wherever these rows are printed: spend is recorded per
-- run, not per item, so per-sender cost can only ever be items × the month's average
-- cost per extracted item. An estimate, clearly labelled as one.
--
-- Params: :user_id, :month_start, :limit
SELECT
  si.author                AS author,
  COUNT(DISTINCT si.id)    AS items_extracted,
  COUNT(c.id)              AS commitments
FROM source_item si
LEFT JOIN commitment c ON c.source_item_id = si.id
WHERE si.user_id = :user_id
  AND si.triage_verdict = 'keep'
  AND si.extraction_version IS NOT NULL
  AND si.occurred_at >= :month_start
GROUP BY si.author
ORDER BY items_extracted DESC
LIMIT :limit;
