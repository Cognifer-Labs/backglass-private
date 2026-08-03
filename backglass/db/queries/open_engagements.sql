-- Live plans, for the dedup/advance pass in extract/engagements.py.
--
-- `proposed` and `confirmed` only. A `declined` plan is a decision the owner already
-- made, and re-reading the thread that arranged it must not quietly revive it — the
-- same reasoning that keeps `done` and `superseded` out of open_commitments_for_dedup.
-- A genuinely new invitation to a thing that was turned down once is a new plan.
--
-- The participants ride along as a comma-joined list of entity ids rather than as extra
-- rows, so one plan is one row and the caller can compare guest lists without a second
-- query per candidate. GROUP_CONCAT order is unspecified in SQLite, so the caller must
-- treat it as a set, which it does.
--
-- Params: :user_id
SELECT
  e.id,
  e.kind,
  e.what,
  e.starts_at,
  e.ends_at,
  e.location,
  e.status,
  e.confidence,
  e.source_item_id,
  GROUP_CONCAT(p.entity_id) AS entity_ids
FROM engagement e
LEFT JOIN engagement_person p
  ON p.engagement_id = e.id AND p.user_id = e.user_id
WHERE e.user_id = :user_id
  AND e.status IN ('proposed', 'confirmed')
GROUP BY e.id;
