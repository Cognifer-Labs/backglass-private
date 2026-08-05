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
-- Grouping: errors are wrapped as they propagate — sync.py prepends `triage <item_id>: `,
-- extract/client.py prepends `model returned an unusable response after N attempts:
-- model reported an error: `. Those two frames are ours, and stripping them leaves the
-- cause the owner can act on. What remains is grouped on itself with digit runs removed,
-- because the digits are precisely what varies while the failure stays the same: item
-- ids, cent counters, AppleEvent error codes. The row rendered is the newest real message
-- in the group, never the digit-stripped key — the key is for counting, not for reading.
--
-- Params: :user_id, :runs
WITH recent AS (
  SELECT id, started_at, errors_json
  FROM run
  WHERE user_id = :user_id AND kind = 'sync'
  ORDER BY id DESC
  LIMIT :runs
),
newest AS (SELECT MAX(id) AS id FROM recent),
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
  FROM recent r, json_each(r.errors_json) j
  WHERE r.errors_json IS NOT NULL
),
unwrapped AS (
  SELECT
    run_id,
    started_at,
    stage,
    CASE WHEN stage = '' THEN full_message
         ELSE substr(full_message, instr(full_message, ': ') + 2) END AS body
  FROM raw
),
cause AS (
  SELECT
    run_id,
    started_at,
    stage,
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
  COUNT(*) AS occurrences,
  COUNT(DISTINCT run_id) AS run_count,
  MAX(run_id) = (SELECT id FROM newest) AS in_latest_run
FROM cause
GROUP BY
  stage,
  replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(
    message, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''),
    '6', ''), '7', ''), '8', ''), '9', '')
ORDER BY last_run_id DESC, occurrences DESC, message;
