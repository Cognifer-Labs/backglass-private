-- Spend and extraction volume by day since :month_start. Feeds the month-end
-- projection in backglass/costs.py.
--
-- Params: :user_id, :month_start
SELECT
  substr(started_at, 1, 10)         AS day,
  COALESCE(SUM(spend_cents), 0)     AS spend_cents,
  COALESCE(SUM(items_extracted), 0) AS extracted
FROM run
WHERE user_id = :user_id
  AND started_at >= :month_start
GROUP BY day
ORDER BY day;
