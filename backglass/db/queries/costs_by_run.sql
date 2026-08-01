-- The last :limit runs, newest first, for `backglass costs runs`.
--
-- Params: :user_id, :limit
SELECT
  started_at,
  items_fetched,
  items_triaged_out,
  items_extracted,
  writes,
  spend_cents,
  degraded,
  errors_json IS NOT NULL AS had_errors
FROM run
WHERE user_id = :user_id
ORDER BY id DESC
LIMIT :limit;
