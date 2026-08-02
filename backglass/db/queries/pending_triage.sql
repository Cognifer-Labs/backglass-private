-- Items the rule layer could not classify, awaiting the tier-1 model.
-- docs/02: rules run first and handle most of it; only survivors reach the model.
--
-- Params: :user_id
SELECT id, source, external_id, occurred_at, author, title, body_text, raw_json,
       template_hash
FROM source_item
WHERE user_id = :user_id
  AND (triage_verdict IS NULL OR triage_verdict = 'unclassified')
ORDER BY datetime(occurred_at) DESC;
