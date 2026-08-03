-- The most recent brief, for /brief. Phase 2's output surface.
--
-- Ordered by the day it was written for and not by id: a brief regenerated for an
-- earlier date takes the higher id without becoming the latest morning.
--
-- Params: :user_id
SELECT
  id, generated_for_date, kind, content_md, word_count, sent_at, opened_at
FROM brief
WHERE user_id = :user_id
ORDER BY date(generated_for_date) DESC, id DESC
LIMIT 1;
