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

1. Private tree committed + `uv run pytest` green.
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
