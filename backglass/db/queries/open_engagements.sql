-- Live plans, for the dedup/advance pass in extract/engagements.py.
--
-- `declined` rides along with `proposed` and `confirmed`, and that is load-bearing
-- rather than tidy — it is the same reasoning that keeps `dropped` inside
-- open_commitments_for_dedup. A declined plan is a decision the owner already made, and
-- the dedup pass is the only thing standing between that decision and the messages that
-- arranged it. Hide it here and re-extraction (which the v4 prompt bump forces over the
-- whole ledger) reads "Dinner Friday?" again, matches nothing, and files a fresh
-- `proposed` row for a dinner that was cancelled — so the brief asks the owner to reply
-- to something they already turned down, and the cancellation message adds a third row.
--
-- Being visible does not make it revivable: engagements.ADVANCES_TO maps `declined` to
-- itself alone, so a later sighting cites the row and cannot move it.
--
-- `done` and `superseded` stay out, exactly as they do for commitments: a plan that
-- happened and is then arranged again is a new plan, and suppressing the second one
-- because it reads like the first is how a standing arrangement disappears.
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
  AND e.status IN ('proposed', 'confirmed', 'declined')
GROUP BY e.id;
