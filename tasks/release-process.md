# Public release process (private-tree only — tasks/ never ships)

The public repo is `contactdharsan-blip/backglass`. This private repo
(`backglass-private`) is the working copy; its history is contaminated with personal
strings and must NEVER be pushed anywhere public, cloned into an export, or touched by
`git archive`/`git filter-repo` for release purposes.

## The boundary

`scripts/release/manifest.py` is the single boundary for what ships:

- `ALLOW_PATHS` — explicit allowlist. A new file that should ship must be added here;
  forgetting is fail-safe (it just doesn't ship).
- `DENY_PATHS` — personal surfaces subtracted even under an allowed prefix:
  `tasks/`, `PROMPT.md`, `docs/13`, `docs/14`, `specs/roadmaps/medical.md`,
  `specs/roadmaps/medical.public.md`, `data/`, `.env`, `.claude/`.
- `SUBSTITUTIONS` — `specs/roadmaps/medical.md` ships the fictional
  `medical.public.md` content at the real path. Keep the staging file's structure in
  sync with the tests that couple to the preset (see tasks/todo.md B11 constraints).

New personal content goes only in already-denied paths, or gets a DENY entry in the
same change that creates it. `tests/test_release_manifest.py` statically guards the
manifest shape.

## Cutting a public update

1. Private tree committed + `uv run pytest` green. **The builder now enforces the
   "committed" half** (`require_clean_worktree`): `git ls-files` cannot see uncommitted
   files, so exporting from a dirty tree omits them while shipping every tracked file
   that depends on them. That is not hypothetical — `backglass/ledger.py` once shipped
   calling `commitment_evidence` while `0013_commitment_evidence.sql` was still
   untracked; the export built, passed the scrub gate, and was broken on first run.
   The guard is scoped to ALLOW_PATHS, so edits under `tasks/` or `data/` never block.
2. `uv run python scripts/release/build_public_repo.py --out /private/tmp/backglass-release/<date>`
   — assembles, runs the scrub gate, `uv sync` + full pytest inside the scratch tree,
   single local commit. It refuses git-based copying by design.
3. Independent re-grep of the scratch tree for the banned terms (list in
   `scripts/release/scrub_gate.py`; treat it as provisional — re-grep broadly, not just
   known terms). Allowed hits: the LICENSE copyright line and the gate's own constants.
4. Diff scratch tree vs public repo checkout; push from the scratch tree (either squash
   to a fresh single commit or start accumulating public history — decide per release).
5. Fresh `git clone` of the pushed repo outside this checkout; run GETTING_STARTED.md
   verbatim; init/setup/doctor/dashboard/sync must degrade per Rule 5.

## Standing rules

- LICENSE is MIT, `Copyright (c) 2026 Dharsan Kesavan` — the one place the real name
  is deliberately public.
- Public persona for all fixtures/examples: Alex Rivera
  (`alex.rivera@example.com` / `arivera@example.edu` / `alexrivera`).
- Owner's real machine pins `MODEL_BACKEND=claude_cli` in `.env`; public code default
  is `anthropic` (BYOK). Don't "fix" either side to match the other.
- launchd jobs on this machine: old `com.cognifer.*` jobs were replaced by
  `com.backglass.*` via `backglass schedule install` — plists are rendered from
  `launchd/templates/`, never hand-edited.

## Building the Mac app

`desktop/build-sidecar.sh` freezes the backend with PyInstaller and bundles it into an
ad-hoc-signed `Backglass.app` (~61MB). It is deliberately NOT Developer-ID signed or
notarized — the distribution decision is open-source/build-it-yourself, so recipients
either build it themselves or right-click → Open past Gatekeeper. GETTING_STARTED.md §2
says so in the recipient's words.

Two things the script now asserts, both because their failure is silent:

- The frozen tree serves `/` and `/design/tokens.css` from a directory outside the repo,
  so a missing `--add-data` entry fails the build rather than the user's first launch.
- Exactly three googleapiclient discovery documents ship. PyInstaller's bundled hook
  collects all 586 (99MB) unless the spec filters them after `Analysis`; the app just
  quietly triples in size, which nobody notices in a diff. The three are the
  (api, version) pairs `__main__._google_service` actually builds — gmail/v1,
  calendar/v3, drive/v3. If a connector ever calls a fourth Google API, add it to
  `USED_DISCOVERY_DOCS` in the spec or discovery falls back to a network fetch.

The `.app` carries no `.env`, no database and no owner strings — release builds run from
`~/Library/Application Support/Backglass` against a fresh ledger. Re-verify with a grep
over the bundle before sending it to anyone.
