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
| `head` | git rev-parse --short HEAD | — |
| `branch` | git rev-parse --abbrev-ref HEAD | — |
| `uncommitted` | git status --porcelain | 2 derivations |
| `commits_ahead_of_upstream` | git rev-list --count @{upstream}..HEAD | unknown-capable |

## `deployed`

| claim | derivation | notes |
| --- | --- | --- |
| `app` | path exists | 2 derivations |
| `matches_source` | sha256 of 48 frozen surfaces and the Python manifest vs the checkout | — |
| `stale_surfaces` | sha256 mismatch vs the checkout | — |
| `stale_python` | sha256 vs backglass-python.sha256 recorded at build time | — |

## `schema`

| claim | derivation | notes |
| --- | --- | --- |
| `applied` | SELECT version FROM schema_version | — |
| `on_disk` | backglass/db/migrations/*.sql | — |
| `unapplied` | migrations on disk with no row | — |

## `prompts`

| claim | derivation | notes |
| --- | --- | --- |
| `on_disk` | frontmatter of specs/extraction-prompts/*.md | — |
| `versions_in_the_ledger` | SELECT DISTINCT extraction_version FROM source_item | — |

## `ledger`

| claim | derivation | notes |
| --- | --- | --- |
| `source_items` | SELECT COUNT(*) FROM source_item | — |
| `open_commitments` | SELECT COUNT(*) FROM commitment WHERE status = 'open' | — |
| `untriaged` | source_item WHERE triage_verdict IS NULL | — |
| `kept_not_extracted` | kept items with no extraction_version — pending or parked | — |

## `pipeline`

| claim | derivation | notes |
| --- | --- | --- |
| `last_run` | SELECT * FROM run ORDER BY id DESC | unknown-capable, 2 derivations |
| `model_calls` | GROUP BY tier over model_call | unknown-capable |

## `knowledge_base`

| claim | derivation | notes |
| --- | --- | --- |
| `facts` | facts.recall(conn) | — |
| `with_provenance` | active facts whose source_item_id is not NULL | — |
| `proposed` | fact WHERE status = 'proposed' — extraction candidates waiting on the owner, invisible to owner_context until accepted | — |
| `config_drift` | facts.config_drift | — |
| `owner_context_chars` | len(facts.owner_context(conn)) — what triage now carries | — |
| `situation_versions` | rows in situation_doc — one per reading that actually differed | 2 derivations |
| `situation_current` | situation.render(...) == the newest stored body; false means the ledger moved since the last sync wrote a version | 2 derivations |

## `open_questions`

| claim | derivation | notes |
| --- | --- | --- |
| `waiting` | open_question WHERE status = 'open' | — |
| `by_kind` | GROUP BY kind over the same rows | — |
| `answered` | status = 'answered' — each also recorded as a decision | — |

## `retrieval`

| claim | derivation | notes |
| --- | --- | --- |
| `indexed` | COUNT over embedding vs kept source_item with body text | — |
| `model` | settings.embedding_model | — |
| `pending` | indexable minus indexed — documents a search cannot reach | — |

## `vault`

| claim | derivation | notes |
| --- | --- | --- |
| `root` | settings.vault_export_path | unknown-capable, 2 derivations |

## `schedule`

| claim | derivation | notes |
| --- | --- | --- |
| `jobs` | com.backglass.*.plist, and the mtime of each job's log | 3 derivations |
| `drifting` | last fire more than 45m from the scheduled time — if every job is off by the same amount the machine's timezone changed since it last booted and only a restart fixes it; if one is off, `backglass schedule install` reloads it | — |
| `timezone` | readlink /etc/localtime | — |

## Configured installs only

Claims a fresh checkout never reaches — an exported vault, a machine with a
configured upstream. Named here so the surface is complete rather than
complete-as-far-as-this-machine-got.

| claim | derivation | source |
| --- | --- | --- |
| `vault.also_ingested` | OBSIDIAN_VAULT_PATH == VAULT_EXPORT_PATH | `state.py:511` |
| `vault.exported_at` | STATE.md frontmatter `generated_at` | `state.py:539` |
| `vault.generated` | — | `state.py:503` |
| `vault.last_run` | MAX(started_at) FROM run | `state.py:544` |
| `vault.notes` | *.md under the vault root | `state.py:502` |

## Verdicts

What `state` *judges*, rather than reports. Forty claims with no verdict left
every reader to know which numbers were bad news; these are the ones that say so
themselves, and each carries the remedy for its own failure.

| verdict |
| --- |
| code.commits_ahead_of_upstream |
| pipeline.last_run |
| pipeline.model_calls |
| vault.root |
| schema is current |
| installed app matches this checkout |
| every scheduled job is loaded |
| every prompt the ledger cites is on disk |
| every kept item is triaged |
| every kept item is extracted |
| config agrees with the knowledge base |
| today has a plan |
| today has a brief |
