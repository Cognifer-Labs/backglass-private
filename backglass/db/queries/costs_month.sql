-- Month-to-date totals for the costs report. Deliberately parallel to
-- spend_this_month.sql, which stays the enforcement query — reporting and
-- enforcement never share a query, so a reporting tweak can never move the cap.
--
-- Params: :user_id, :month_start
SELECT
  COALESCE(SUM(spend_cents), 0)     AS spend_cents,
  COUNT(*)                          AS runs,
  COALESCE(SUM(degraded), 0)        AS degraded_runs,
  COALESCE(SUM(items_extracted), 0) AS extracted,
  COALESCE(SUM(items_fetched), 0)   AS fetched,
  COALESCE(SUM(items_triaged_out), 0) AS triaged_out
FROM run
WHERE user_id = :user_id
  AND started_at >= :month_start;
