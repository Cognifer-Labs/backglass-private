-- People search. Phase 6. LIKE, not FTS — entity count is hundreds at personal
-- scale, and an index that must be kept in sync buys nothing here (ruling in
-- tasks/todo.md §Phase 6).
--
-- `last_touch_at` is commitment-derived: the newest source item behind any
-- commitment with this counterparty. Author-only mentions count in the profile
-- timeline but not here, so the list and the cold flag stay one consistent metric.
--
-- `curated` marks profiles the owner has invested in (role, org, or tags). Only
-- those are eligible for going-cold treatment.
--
-- Params: :user_id, :q (lowercased '%text%', or NULL), :tag ('%"tag"%', or NULL)
SELECT
  e.id, e.kind, e.canonical_name, e.aliases_json, e.role, e.org, e.tags_json, e.notes,
  (SELECT MAX(s.occurred_at) FROM commitment c
     JOIN source_item s ON s.id = c.source_item_id
   WHERE c.counterparty_entity_id = e.id)                                AS last_touch_at,
  (SELECT COUNT(*) FROM commitment c
   WHERE c.counterparty_entity_id = e.id AND c.status = 'open')          AS open_count,
  (e.role IS NOT NULL OR e.org IS NOT NULL OR e.tags_json != '[]')      AS curated
FROM entity e
WHERE e.user_id = :user_id
  AND e.kind IN ('person', 'org')
  AND (:q IS NULL
       OR lower(e.canonical_name)      LIKE :q
       OR lower(e.aliases_json)        LIKE :q
       OR lower(COALESCE(e.role, ''))  LIKE :q
       OR lower(COALESCE(e.org, ''))   LIKE :q
       OR lower(e.tags_json)           LIKE :q)
  AND (:tag IS NULL OR e.tags_json LIKE :tag)
ORDER BY last_touch_at IS NULL, last_touch_at DESC, e.canonical_name;
