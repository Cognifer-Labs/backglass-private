-- docs/06 §Panels, Today: "Proposed plan blocks, protected block marked, capacity line."
--
-- Written by the day planner in Phase 4. Empty until then, and the panel renders its
-- declarative empty state rather than a placeholder.
--
-- Params: :user_id, :local_date
SELECT
  b.id, b.starts_at, b.ends_at, b.kind, b.title, b.pinned, b.outcome,
  b.rollover_count, b.commitment_id, b.goal_id,
  p.id AS day_plan_id, p.capacity_minutes, p.planned_minutes, p.overflow_count,
  p.overflow_dated,
  p.tz, p.status AS plan_status,
  -- What has to be open before the block can start: the chapters, the browser the exam
  -- will not run without, the guide it links to (migration 0031). Read through the
  -- source item, which is the only thing a commitment and its assignment share.
  -- Ordered inside the subquery so the line reads the same on every render.
  (
    SELECT group_concat(name, ' · ') FROM (
      SELECT m.name AS name
      FROM assignment_material m
      JOIN assignment a ON a.id = m.assignment_id
      JOIN commitment c ON c.source_item_id = a.source_item_id
      WHERE c.id = b.commitment_id AND m.user_id = :user_id
      ORDER BY m.kind, m.name
    )
  ) AS needs
FROM plan_block b
JOIN day_plan p ON p.id = b.day_plan_id
WHERE p.user_id = :user_id
  AND p.local_date = :local_date
  AND p.status != 'superseded'
ORDER BY b.starts_at;
