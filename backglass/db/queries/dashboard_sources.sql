-- docs/06 §The Sources panel: "Looks like ops chrome, is actually the most important panel."
--
-- Per source: status, name, relative timestamp of last successful sync, item count. On
-- failure the row's keyline turns vermilion and stays that way until fixed — which is why
-- `status` is read straight from the credential row rather than inferred from recency.
--
-- docs/11 §8: "The dangerous failure is not the error, it is a brief that looks complete
-- and is not." This query is what makes the difference visible.
--
-- Params: :user_id
SELECT
  c.source,
  c.status,
  c.enabled,
  c.last_error,
  c.updated_at,
  (SELECT COUNT(*) FROM source_item s
   WHERE s.user_id = c.user_id AND s.source = c.source) AS item_count,
  (SELECT MAX(s.fetched_at) FROM source_item s
   WHERE s.user_id = c.user_id AND s.source = c.source) AS last_item_at
FROM credential c
WHERE c.user_id = :user_id
ORDER BY c.enabled DESC, (c.status != 'ok') DESC, c.source;
