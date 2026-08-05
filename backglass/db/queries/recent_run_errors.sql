-- What has been going wrong lately, grouped so eleven copies of one failure read as one.
--
-- `run.errors_json` is where the pipeline records everything CLAUDE.md rule 5 says must
-- degrade rather than block: a connector that could not fetch, an item that failed
-- triage or extraction, the spend cap tripping. Until this query existed nothing in the
-- web layer read it — `backglass status` printed the last run's array and the next sync,
-- thirty minutes later, overwrote it. On 2026-08-05 the owner's scheduled syncs failed
-- with a growing pile of "OAuth session expired and could not be refreshed", 1 error at
-- 12:58 and 11 by 16:58, and no surface said so. It healed by accident.
--
-- Window: the last :runs sync runs, not a wall-clock span. A time window has the wrong
-- failure mode — when the scheduler itself dies, "the last 24 hours" goes quiet exactly
-- when the ledger is most wrong, and the errors that explain the silence disappear with
-- it. A run count always covers real recent activity whatever the cadence. Sync runs
-- only, for heartbeat.py's reason: `run` records other kinds (roadmap interviews), the
-- panel speaks about syncing, so the query has to mean it.
--
-- `window_runs` is the size of the window that was ACTUALLY read, which is `min(:runs,
-- runs in the ledger)` and not :runs. A surface that prints the constant says "in 3 of
-- the last 20 syncs" about a four-run ledger where three of four failed: 15% rendered
-- for 75%, severity inverted on precisely the young-ledger and dead-scheduler cases this
-- window shape exists to protect. The count is returned so no caller has to guess.
--
-- Grouping: errors are wrapped as they propagate — sync.py prepends `triage <item_id>: `,
-- extract/client.py prepends `model returned an unusable response after N attempts:
-- model reported an error: `. Those two frames are ours, and stripping them leaves the
-- cause the owner can act on. What remains is grouped on itself with digit runs removed,
-- because the digits are precisely what varies while the failure stays the same: item
-- ids, cent counters, AppleEvent error codes. The row rendered is the newest real message
-- in the group, never the digit-stripped key — the key is for counting, not for reading.
--
-- `items` counts DISTINCT source_items, `occurrences` counts error records. They are not
-- the same number and the difference is the whole point: the owner's 40 OAuth records
-- were 11 items re-attempted every half hour for four hours. Reporting 40 inflates the
-- damage 3.6x, and the inflation GROWS with how long a small fault runs — the longer a
-- persistent bug lasts, the larger the crisis the panel invents. The id is parsed out of
-- the prefix before it is stripped for grouping, because after stripping it is gone.
--
-- Params: :user_id, :runs
WITH recent AS (
  SELECT id, started_at, errors_json
  FROM run
  WHERE user_id = :user_id AND kind = 'sync'
  ORDER BY id DESC
  LIMIT :runs
),
window_size AS (SELECT COUNT(*) AS runs, MAX(id) AS newest_id FROM recent),
raw AS (
  SELECT
    r.id AS run_id,
    r.started_at,
    -- The stage that owns the item, when sync.py framed the error with one. Checked
    -- against a digit so an error whose text merely opens with the word is left alone.
    CASE
      WHEN j.value LIKE 'triage %'  AND substr(j.value, 8, 1) BETWEEN '0' AND '9' THEN 'triage'
      WHEN j.value LIKE 'extract %' AND substr(j.value, 9, 1) BETWEEN '0' AND '9' THEN 'extract'
      WHEN j.value LIKE 'apply %'   AND substr(j.value, 7, 1) BETWEEN '0' AND '9' THEN 'apply'
      ELSE ''
    END AS stage,
    j.value AS full_message
  -- sync.py only ever writes json.dumps(list), so the guard is unreachable today. It is
  -- here because json_each RAISES on a malformed value, and an exception inside the
  -- Sources panel takes down all seven panels: the dashboard would go dark over one bad
  -- row in a column whose entire job is to report that something went wrong.
  FROM recent r,
       json_each(CASE WHEN json_valid(r.errors_json)
                       AND json_type(r.errors_json) = 'array'
                      THEN r.errors_json ELSE '[]' END) j
),
unwrapped AS (
  SELECT
    run_id,
    started_at,
    stage,
    -- The source_item this error is about, kept before the prefix is thrown away.
    CASE WHEN stage = '' OR instr(full_message, ': ') = 0 THEN NULL
         ELSE substr(full_message, length(stage) + 2,
                     instr(full_message, ': ') - length(stage) - 2) END AS item_id,
    CASE WHEN stage = '' THEN full_message
         ELSE substr(full_message, instr(full_message, ': ') + 2) END AS body
  FROM raw
),
cause AS (
  SELECT
    run_id,
    started_at,
    stage,
    item_id,
    -- 25 = length('model reported an error: ').
    CASE WHEN instr(body, 'model reported an error: ') > 0
         THEN substr(body, instr(body, 'model reported an error: ') + 25)
         ELSE body END AS message
  FROM unwrapped
)
SELECT
  -- Exactly one min/max aggregate, so SQLite's bare columns are read off the newest
  -- occurrence: the message shown is the one that actually happened most recently.
  MAX(run_id) AS last_run_id,
  started_at AS last_at,
  stage,
  message,
  COUNT(DISTINCT item_id) AS items,
  COUNT(*) AS occurrences,
  COUNT(DISTINCT run_id) AS run_count,
  (SELECT runs FROM window_size) AS window_runs,
  MAX(run_id) = (SELECT newest_id FROM window_size) AS in_latest_run
FROM cause
GROUP BY
  stage,
  replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(
    message, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''),
    '6', ''), '7', ''), '8', ''), '9', '')
ORDER BY last_run_id DESC, occurrences DESC, message;
