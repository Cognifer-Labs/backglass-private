-- Weekly triage kill rate over the trailing weeks, newest first.
--
-- Same 85% drift threshold as triage_kill_rate.sql, same exclusion of
-- structured-source drops (they were never model candidates).
--
-- Params: :user_id, :weeks
SELECT
  strftime('%Y-%W', fetched_at)                              AS week,
  COUNT(*)                                                   AS total,
  SUM(CASE WHEN triage_verdict = 'drop' THEN 1 ELSE 0 END)   AS dropped,
  CAST(SUM(CASE WHEN triage_verdict = 'drop' THEN 1 ELSE 0 END) AS REAL)
    / NULLIF(COUNT(*), 0)                                    AS kill_rate
FROM source_item
WHERE user_id = :user_id
  AND triage_verdict IS NOT NULL
  AND (triage_reason IS NULL OR triage_reason NOT LIKE 'structured source%')
GROUP BY week
ORDER BY week DESC
LIMIT :weeks;
