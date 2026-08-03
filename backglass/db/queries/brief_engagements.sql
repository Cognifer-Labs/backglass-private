-- Plans on the horizon, for the brief's "Plans" section.
--
-- Two states, and the difference is the point. A `confirmed` plan is a fact about the
-- week the owner should be reminded of; a `proposed` one is a question they have not
-- answered, and an invitation nobody replies to is the failure mode this record type
-- exists to catch. Both ride in one section so the owner reads their week in one place.
--
-- Undated plans are included and sort last: "we should get dinner" has no date by
-- definition, and it is precisely the plan that decays silently. Nothing else in the
-- brief would ever surface it.
--
-- The participants arrive as one comma-joined string of names rather than as several
-- rows, so one plan is one line. GROUP_CONCAT's order is unspecified, so the caller must
-- not read anything into it beyond membership.
--
-- Only plans at or above the confidence threshold. CLAUDE.md rule 2: a low-confidence
-- extraction goes to the review queue, never into the brief as fact.
--
-- The horizon compares (and the ordering sorts) on `substr(starts_at, 1, 10)` rather
-- than through date()/datetime(). This column holds the time as the message stated it —
-- the resolver never converts timezones — so bare dates, naive local datetimes and
-- offset-bearing ones all live here together. date() normalises the offset-bearing rows
-- to UTC first, which walks a 19:00 Phoenix plan into the following day; the leading ten
-- characters are the local day under all three shapes and convert nothing. See
-- tasks/lessons.md, 2026-08-01.
--
-- Params: :user_id, :horizon (ISO date), :confidence_threshold
SELECT
  e.id,
  e.kind,
  e.what,
  e.starts_at,
  e.location,
  e.status,
  e.confidence,
  e.source_item_id,
  GROUP_CONCAT(p.canonical_name, ', ') AS people,
  s.source,
  s.external_id AS source_external_id,
  s.occurred_at AS source_occurred_at,
  s.title       AS source_title
FROM engagement e
JOIN source_item s ON s.id = e.source_item_id
LEFT JOIN engagement_person ep
  ON ep.engagement_id = e.id AND ep.user_id = e.user_id
LEFT JOIN entity p ON p.id = ep.entity_id
WHERE e.user_id = :user_id
  AND e.status IN ('proposed', 'confirmed')
  AND e.confidence >= :confidence_threshold
  AND (e.starts_at IS NULL OR substr(e.starts_at, 1, 10) <= :horizon)
GROUP BY e.id
ORDER BY (e.starts_at IS NULL) ASC, substr(e.starts_at, 1, 10) ASC, e.id ASC;
