-- docs/05 §Failure states: "A source has been failing for more than one cycle: name it at
-- the top. 'Gmail auth expired 3 days ago, this brief is incomplete.'"
--
-- docs/07: auth expiry is a visible product state, not a log line. This is the query that
-- makes it visible in the one place the owner reliably looks.
--
-- Params: :user_id, :today
SELECT
  source, status, last_error, updated_at,
  CAST(julianday(date(:today)) - julianday(date(updated_at)) AS INTEGER) AS days_failing
FROM credential
WHERE user_id = :user_id
  AND status != 'ok'
  -- A source the owner paused is not failing; the brief stays quiet about it.
  AND enabled = 1
ORDER BY updated_at ASC;
