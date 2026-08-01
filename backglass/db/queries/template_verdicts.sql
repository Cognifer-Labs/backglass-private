-- Prior verdicts among an item's template siblings. The rule layer drops the item
-- only when every prior sibling was dropped (rule or model) and there are at least
-- `template_drop_after` (config) of them — one keep, ever, disqualifies, because triage
-- precision is not for sale (triage.md).
--
-- Params: :user_id, :template_hash, :id
SELECT
  COALESCE(SUM(CASE WHEN triage_verdict = 'drop' THEN 1 ELSE 0 END), 0) AS dropped,
  COALESCE(SUM(CASE WHEN triage_verdict = 'keep' THEN 1 ELSE 0 END), 0) AS kept
FROM source_item
WHERE user_id = :user_id
  AND template_hash = :template_hash
  AND id != :id
  AND triage_verdict IS NOT NULL;
