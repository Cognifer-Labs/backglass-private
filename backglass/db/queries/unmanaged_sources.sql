-- Sources that have evidence in the ledger but no credential row behind them.
--
-- `dashboard_sources.sql` reads FROM credential, so it can only show sources a connector
-- owns. Anything else — `manual` quick-adds, a one-off import script, a source whose
-- credential row was deleted — contributes source_items that the brief and the planner
-- cite, while appearing nowhere in the Sources panel. docs/11 §8: "The dangerous failure
-- is not the error, it is a brief that looks complete and is not." An invisible evidence
-- feed that no sync will ever refresh is exactly that shape, so it gets a row.
--
-- Not a failure state and not paused: there is nothing to reconnect and nothing to
-- resume. The panel renders these as quiet informational rows.
--
-- Params: :user_id
SELECT
  s.source,
  COUNT(*) AS item_count,
  MAX(s.fetched_at) AS last_item_at
FROM source_item s
WHERE s.user_id = :user_id
  AND NOT EXISTS (
    SELECT 1 FROM credential c
    WHERE c.user_id = s.user_id AND c.source = s.source
  )
GROUP BY s.source
ORDER BY item_count DESC, s.source;
