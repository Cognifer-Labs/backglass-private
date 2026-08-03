-- Every plan one person is part of, for their profile page.
--
-- All statuses, unlike the brief's query: a profile is a record of a relationship, and
-- "you cancelled the last three" is exactly the kind of thing the owner opened the page
-- to find out. The caller separates upcoming from past.
--
-- The other guests ride along so a line can read "dinner with Priya and Sam" on Sam's
-- page too. The person whose page this is is excluded from that list — they are the
-- subject, not a guest at their own entry.
--
-- Sorted newest-first on the local date prefix rather than through datetime(): the
-- column holds the time as the message stated it, mixing bare dates with naive and
-- offset-bearing datetimes, and datetime() would normalise the offsets to UTC and
-- reorder evening plans across their own day boundary (tasks/lessons.md, 2026-08-01).
-- Undated plans sort last; they have no place on a timeline but must not vanish.
--
-- Params: :user_id, :entity_id
SELECT
  e.id,
  e.kind,
  e.what,
  e.starts_at,
  e.location,
  e.status,
  e.confidence,
  e.source_item_id,
  (
    SELECT GROUP_CONCAT(o.canonical_name, ', ')
    FROM engagement_person op
    JOIN entity o ON o.id = op.entity_id
    WHERE op.engagement_id = e.id
      AND op.user_id = e.user_id
      AND op.entity_id != :entity_id
  ) AS others,
  s.source,
  s.external_id AS source_external_id,
  s.occurred_at AS source_occurred_at,
  s.title       AS source_title
FROM engagement e
JOIN engagement_person p
  ON p.engagement_id = e.id AND p.user_id = e.user_id
JOIN source_item s ON s.id = e.source_item_id
WHERE e.user_id = :user_id
  AND p.entity_id = :entity_id
ORDER BY (e.starts_at IS NULL) ASC, substr(e.starts_at, 1, 10) DESC, e.id DESC;
