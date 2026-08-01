-- All triaged mail-shaped items, for learned-noise mining. Grouped in Python
-- (backglass/extract/noise.py) rather than SQL because sender parsing must match
-- rules._address — email.utils.parseaddr — which SQL cannot express.
--
-- Params: :user_id
SELECT id, author, triage_verdict, triage_reason, occurred_at
FROM source_item
WHERE user_id = :user_id
  AND triage_verdict IS NOT NULL
  AND author LIKE '%@%';
