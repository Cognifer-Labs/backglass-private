-- Every owner correction of an extracted commitment, in one window.
--
-- web/actions.py writes the owner's review-queue verdict into `resolution_note` in two
-- parseable shapes and nowhere else:
--
--   accept  →  'accepted by owner (model said 0.62)'   (row stays open, confidence → 1.0)
--   reject  →  'rejected:wrong_date'                   (row is tombstoned as dropped)
--
-- That asymmetry is the reason this query looks the way it does:
--
-- 1. The accept path OVERWRITES `commitment.confidence` with 1.0, so the column no
--    longer holds what the model said. The only surviving copy of the model's score for
--    an accepted row is the text inside the note, which is why the raw note ships out of
--    here unparsed and corrections.py pulls the number back out in Python. Parsing a
--    float out of a string in SQL would be worse in every way, including honesty about
--    what happens when the format drifts.
--
-- 2. The accept path never stamps `resolved_at` — accept is a promotion, not a
--    resolution, and the commitment stays open. So there is no recorded moment at which
--    an accept happened, and the trailing window has to fall back to `created_at`, the
--    moment the extraction landed. For a review queue the owner clears daily those are
--    within a day of each other; for a queue left to rot for a month they are not, and
--    an accept can therefore fall outside a window it belongs in. Stated here rather
--    than papered over: the fix is a timestamp column, not a cleverer WHERE clause.
--
-- `status` is deliberately NOT filtered. A reject is 'dropped' and an accept is 'open',
-- and filtering on either one silently halves the sample — which is exactly the sort of
-- quiet bias that makes a calibration table lie.
--
-- Params: :user_id, :since  (ISO timestamp; the start of the trailing window)
SELECT
  c.id                                        AS commitment_id,
  c.resolution_note                           AS resolution_note,
  c.confidence                                AS confidence,
  COALESCE(c.resolved_at, c.created_at)       AS corrected_at,
  si.source                                   AS source,
  si.author                                   AS author,
  si.extraction_version                       AS extraction_version
FROM commitment c
JOIN source_item si ON si.id = c.source_item_id
WHERE c.user_id = :user_id
  AND c.resolution_note IS NOT NULL
  AND (c.resolution_note LIKE 'rejected:%' OR c.resolution_note LIKE 'accepted by owner%')
  AND COALESCE(c.resolved_at, c.created_at) >= :since
ORDER BY corrected_at DESC;
