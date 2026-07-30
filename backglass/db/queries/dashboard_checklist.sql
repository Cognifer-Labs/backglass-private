-- docs/06 §Panels, Checklist: "Today's non-negotiables, binary ticks."
--
-- `weekday_mask` is a bitmask with bit 0 = Monday, per specs/schema.sql. The seven-item
-- cap is enforced in application code so the error message can explain why (docs/04 C1).
--
-- Params: :user_id, :local_date, :weekday_bit
SELECT
  i.id, i.title, i.sort_order,
  t.id AS tick_id, t.ticked_at
FROM checklist_item i
LEFT JOIN checklist_tick t
       ON t.checklist_item_id = i.id AND t.local_date = :local_date
WHERE i.user_id = :user_id
  AND i.active = 1
  AND (i.weekday_mask & :weekday_bit) != 0
ORDER BY i.sort_order, i.id;
