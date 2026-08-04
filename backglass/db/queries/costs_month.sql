-- Month-to-date totals for the costs report. Deliberately parallel to
-- spend_this_month.sql, which stays the enforcement query — reporting and
-- enforcement never share a query, so a reporting tweak can never move the cap.
--
-- Params: :user_id, :month_start
SELECT
  COALESCE(SUM(spend_cents), 0)     AS spend_cents,
  COUNT(*)                          AS runs,
  COALESCE(SUM(degraded), 0)        AS degraded_runs,
  -- Split by cause (migration 0016), because `degraded` alone made the summary tell every
  -- pause as the spend cap's — including a run the subscription's usage window stopped,
  -- which has nothing to do with the cap and clears in hours rather than on the 1st.
  -- NULL predates the column, when the cap was the only thing that could pause a run.
  COALESCE(SUM(degraded AND COALESCE(degrade_reason, 'spend_cap') LIKE 'rate_limit%'), 0)
                                    AS rate_limited_runs,
  COALESCE(SUM(degraded AND COALESCE(degrade_reason, 'spend_cap') NOT LIKE 'rate_limit%'), 0)
                                    AS capped_runs,
  COALESCE(SUM(items_extracted), 0) AS extracted,
  COALESCE(SUM(items_fetched), 0)   AS fetched,
  COALESCE(SUM(items_triaged_out), 0) AS triaged_out
FROM run
WHERE user_id = :user_id
  AND started_at >= :month_start;
