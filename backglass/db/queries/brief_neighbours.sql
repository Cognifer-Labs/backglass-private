-- The nearest briefs either side of a day, for the /brief prev/next links.
--
-- Nearest existing brief, not the calendar neighbour: the generator does not run every
-- day (a missed launchd run, a machine that was asleep), and a "prev" pointing at a day
-- with nothing on it is a link to a 404.
--
-- Params: :user_id, :on_date
SELECT
  (SELECT max(date(generated_for_date)) FROM brief
    WHERE user_id = :user_id AND date(generated_for_date) < date(:on_date)) AS prev_date,
  (SELECT min(date(generated_for_date)) FROM brief
    WHERE user_id = :user_id AND date(generated_for_date) > date(:on_date)) AS next_date;
