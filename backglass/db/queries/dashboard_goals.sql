-- docs/06 §Panels, Goals: "Targets with weekly progress, staleness chips, risk projections."
--
-- Progress is checkpoints in the current week against the target's weekly_count. Staleness
-- and risk are computed independently in Phase 4 (docs/09 §Phase 4 item 5); this query
-- carries the raw counts they will be derived from.
--
-- Params: :user_id, :week_start
SELECT
  g.id AS goal_id, g.title AS goal_title, g.horizon, g.target_date, g.definition_of_done,
  t.id AS target_id, t.kind, t.title AS target_title, t.weekly_count,
  t.estimated_minutes_each,
  (SELECT COUNT(*) FROM checkpoint cp
    WHERE cp.target_id = t.id AND date(cp.occurred_at) >= date(:week_start)) AS done_this_week,
  (SELECT MAX(cp.occurred_at) FROM checkpoint cp WHERE cp.target_id = t.id) AS last_checkpoint
FROM goal g
LEFT JOIN target t ON t.goal_id = g.id AND t.active = 1
WHERE g.user_id = :user_id AND g.status = 'active'
ORDER BY g.target_date IS NULL, g.target_date, g.id, t.id;
