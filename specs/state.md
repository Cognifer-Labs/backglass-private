# What `backglass state` reports

GENERATED from `backglass/state.py` — do not hand-edit. Add or move a claim, then run:

    uv run python -m tests.test_state_reference

`tests/test_state_reference.py` fails if this file and the module disagree, so a section
cannot be dropped by a merge without something saying so.

Every row is one claim and the derivation behind it. **The values are not here** — they
change on every run and belong to the command, not to a document. What belongs here is the
question each field answers and how you would check it yourself, which is the property
CLAUDE.md leans on when it says to prefer this command to inference.

A claim marked *unknown-capable* has a probe that can fail to run. When it does, `state`
says `unknown` and why, and never a zero — a confident answer assembled from a missing
input is the failure the whole command exists to prevent.

A claim marked *configured installs only* is real but unreachable on a fresh checkout, so
its derivation is read from the source rather than from a run.


## `code`

| claim | derivation | notes |
| --- | --- | --- |
| `head` | git rev-parse --short HEAD | unknown-capable, `_code` |
| `branch` | git rev-parse --abbrev-ref HEAD | unknown-capable, `_code` |
| `uncommitted` | git status --porcelain | unknown-capable, `_code` |
| `commits_ahead_of_upstream` | git rev-list --count @{upstream}..HEAD | unknown-capable, `_code` |

## `deployed`

| claim | derivation | notes |
| --- | --- | --- |
| `app` | path exists | unknown-capable, `_deployed` |
| `matches_source` | sha256 of every frozen surface and the Python manifest vs the checkout | unknown-capable, `_deployed` |
| `stale_surfaces` | sha256 mismatch vs the checkout | `_deployed` |
| `stale_python` | sha256 vs backglass-python.sha256 recorded at build time | unknown-capable, `_deployed` |

## `schema`

| claim | derivation | notes |
| --- | --- | --- |
| `applied` | SELECT version FROM schema_version | `_schema` |
| `on_disk` | backglass/db/migrations/*.sql | `_schema` |
| `unapplied` | migrations on disk with no row | `_schema` |

## `prompts`

| claim | derivation | notes |
| --- | --- | --- |
| `on_disk` | frontmatter of specs/extraction-prompts/*.md | `_prompts` |
| `versions_in_the_ledger` | SELECT DISTINCT extraction_version FROM source_item | `_prompts` |

## `ledger`

| claim | derivation | notes |
| --- | --- | --- |
| `source_items` | SELECT COUNT(*) FROM source_item | `_ledger` |
| `open_commitments` | SELECT COUNT(*) FROM commitment WHERE status = 'open' | `_ledger` |
| `untriaged` | source_item WHERE triage_verdict IS NULL | `_ledger` |
| `kept_not_extracted` | kept items with no extraction_version — pending or parked | `_ledger` |

## `pipeline`

| claim | derivation | notes |
| --- | --- | --- |
| `last_run` | SELECT * FROM run ORDER BY id DESC <br> newest row in `run` | unknown-capable, `_pipeline` |
| `model_calls` | GROUP BY tier over model_call | unknown-capable, `_pipeline` |

## `knowledge_base`

| claim | derivation | notes |
| --- | --- | --- |
| `facts` | facts.recall(conn) | `_knowledge_base` |
| `with_provenance` | active facts whose source_item_id is not NULL | `_knowledge_base` |
| `proposed` | fact WHERE status = 'proposed' — extraction candidates waiting on the owner, invisible to owner_context until accepted | `_knowledge_base` |
| `config_drift` | facts.config_drift | `_knowledge_base` |
| `owner_context_chars` | len(facts.owner_context(conn)) — what triage now carries | `_knowledge_base` |
| `situation_versions` | rows in situation_doc — one per reading that actually differed <br> rows in situation_doc | unknown-capable, `_knowledge_base` |
| `situation_current` | situation.render(...) == the newest stored body; false means the ledger moved since the last sync wrote a version <br> situation.render(...) == the newest stored body | unknown-capable, `_knowledge_base` |

## `open_questions`

| claim | derivation | notes |
| --- | --- | --- |
| `waiting` | open_question WHERE status = 'open' | `_open_questions` |
| `by_kind` | GROUP BY kind over the same rows | `_open_questions` |
| `answered` | status = 'answered' — each also recorded as a decision | `_open_questions` |

## `retrieval`

| claim | derivation | notes |
| --- | --- | --- |
| `indexed` | COUNT over embedding vs kept source_item with body text | `_retrieval` |
| `model` | settings.embedding_model | `_retrieval` |
| `pending` | indexable minus indexed — documents a search cannot reach | `_retrieval` |

## `vault`

| claim | derivation | notes |
| --- | --- | --- |
| `root` | settings.vault_export_path | unknown-capable, `_vault` |

## `schedule`

| claim | derivation | notes |
| --- | --- | --- |
| `jobs` | com.backglass.*.plist, and the mtime of each job's log | unknown-capable, `_schedule` |
| `drifting` | last fire more than 45m from the scheduled time — if every job is off by the same amount the machine's timezone changed since it last booted and only a restart fixes it; if one is off, `backglass schedule install` reloads it | `_schedule` |
| `timezone` | readlink /etc/localtime | `_schedule` |

## Configured installs only

Claims a fresh checkout never reaches — an exported vault, chiefly. Named
here so the surface is complete rather than complete-as-far-as-this-machine-
got, and read from the source rather than from a run.

| claim | derivation | notes |
| --- | --- | --- |
| `vault.also_ingested` | OBSIDIAN_VAULT_PATH == VAULT_EXPORT_PATH | `_vault` |
| `vault.exported_at` | STATE.md frontmatter `generated_at` <br> STATE.md frontmatter | unknown-capable, `_vault` |
| `vault.generated` | — | `_vault` |
| `vault.last_run` | MAX(started_at) FROM run | `_vault` |
| `vault.notes` | *.md under the vault root | unknown-capable, `_vault` |

## Verdicts

What `state` *judges*, rather than reports. Forty claims with no verdict left
every reader to know which numbers were bad news; these are the ones that say so
themselves, and each carries the remedy for its own failure.

The named checks only. `verdicts()` also echoes one row per claim that came back
`unknown` on the run that produced it, and those depend on the machine — a
checkout with an upstream configured has no `code.commits_ahead_of_upstream`
row, so including them made this document disagree with itself across two
clones.

| verdict |
| --- |
| schema is current |
| installed app matches this checkout |
| every scheduled job is loaded |
| every prompt the ledger cites is on disk |
| every kept item is triaged |
| every kept item is extracted |
| config agrees with the knowledge base |
| today has a plan |
| today has a brief |
