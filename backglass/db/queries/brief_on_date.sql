-- One day's brief, for /brief/<date>.
--
-- A day holds at most one brief in practice — W1 makes Monday planning replace the
-- daily brief rather than accompany it — but the UNIQUE key is (user_id, date, kind),
-- so a kind switched mid-day can leave two rows. The newest wins, which is the one
-- that was actually sent.
--
-- Params: :user_id, :on_date
SELECT
  id, generated_for_date, kind, content_md, word_count, sent_at, opened_at
FROM brief
WHERE user_id = :user_id
  AND date(generated_for_date) = date(:on_date)
ORDER BY id DESC
LIMIT 1;
