-- Triage kill rate, for the Sources panel.
--
-- docs/02: "Expect tier 1 to eliminate 90 to 95 percent of volume. If it does not, the
-- rules are too permissive and the cost model breaks." Below 85 percent is the signal
-- that the rule layer has drifted and cost is about to climb.
--
-- Params: :user_id
SELECT
  COUNT(*)                                                   AS total,
  SUM(CASE WHEN triage_verdict = 'drop' THEN 1 ELSE 0 END)   AS dropped,
  SUM(CASE WHEN triage_verdict = 'keep' THEN 1 ELSE 0 END)   AS kept,
  CAST(SUM(CASE WHEN triage_verdict = 'drop' THEN 1 ELSE 0 END) AS REAL)
    / NULLIF(COUNT(*), 0)                                    AS kill_rate
FROM source_item
WHERE user_id = :user_id
  AND triage_verdict IS NOT NULL;
