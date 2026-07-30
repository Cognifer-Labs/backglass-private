-- Spend so far in the current calendar month, in cents.
--
-- docs/02 §Cost control: the cap is enforced in code, not monitored. This is the number
-- the enforcement reads before every model call.
--
-- Params: :user_id, :month_start  (ISO timestamp of the first instant of the month)
SELECT COALESCE(SUM(spend_cents), 0) AS spend_cents
FROM run
WHERE user_id = :user_id
  AND started_at >= :month_start;
