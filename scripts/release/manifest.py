"""Boundary between the private tree and the public export.

This is the single source of truth for what ships in the public `backglass` repo.
An allowlist, not a denylist: a new personal file added six months from now and
never added here simply does not ship (annoying, not dangerous) rather than
silently leaking (dangerous). `DENY_PATHS` subtracts from `ALLOW_PATHS` for the
rare case where a broad allow prefix (e.g. `specs/roadmaps/`) needs one file
carved back out. `SUBSTITUTIONS` swaps in replacement content for a path without
touching the private tree's real file.

Re-verify this list against `git ls-files` whenever the tracked tree changes —
see `tests/test_release_manifest.py` for the static checks that catch drift in
the DENY/SUBSTITUTIONS relationship, and `build_public_repo.py --dry-run` for a
human eyeball pass against the live tree.
"""

from __future__ import annotations

ALLOW_PATHS = [
    "backglass/",  # entire package
    "tests/",  # entire test suite (scrubbed of PII in Phase B)
    "docs/01-product-brief.md",
    "docs/02-architecture.md",
    "docs/03-data-model.md",
    "docs/04-daily-schedule-and-goals.md",
    "docs/05-morning-brief.md",
    "docs/06-dashboard.md",
    "docs/07-connectors.md",
    "docs/08-privacy-and-data-boundary.md",
    "docs/09-build-plan.md",
    "docs/10-tech-stack.md",
    "docs/11-ux-flows.md",
    "docs/12-source-extraction-research.md",
    "design/",  # entire design system
    "specs/schema.sql",
    "specs/extraction-prompts/",  # backglass/extract/prompts.py loads these at runtime
    "specs/roadmaps/",  # minus medical.md and medical.public.md, see DENY_PATHS
    "launchd/README.md",
    "launchd/templates/",
    "desktop/",  # entire, already clean of personal strings
    "evals/",  # not in the first draft's manifest; re-verified 2026-08-02 against
    # `git ls-files` and found untracked-for. Contains no PII (checked by grep
    # against the full scrub-gate term list), is referenced by
    # docs/10-tech-stack.md's testing section which already ships, and documents
    # a real, useful mechanism (evals vs. tests) a stranger benefits from seeing.
    # Explicit call: allow.
    "scripts/release/",  # ships itself so a public fork inherits the same tooling
    "scripts/_validator_core.js",
    "scripts/validate-palette.mjs",
    "README.md",
    "GETTING_STARTED.md",
    "LICENSE",
    "CLAUDE.md",
    "pyproject.toml",
    "uv.lock",
    ".gitignore",
    ".env.example",
]

DENY_PATHS = [  # subtracted even if under an ALLOW_PATHS prefix
    "tasks/",
    "PROMPT.md",
    "docs/13-activation-runbook.md",
    "docs/14-med-student-prd.md",
    "specs/roadmaps/medical.md",
    "specs/roadmaps/medical.public.md",  # B11: "private-tree-only staging path" —
    # its content ships (substituted in at medical.md's path), the staging file
    # itself does not. Not in the plan's literal C1 snippet, which only denied
    # medical.md; added here to honor B11's explicit "private-tree-only" framing
    # rather than shipping a redundant duplicate file in the export.
    "data/",
    ".env",
    ".claude/",
]

SUBSTITUTIONS = {  # path in the export tree -> source path to copy from instead
    "specs/roadmaps/medical.md": "specs/roadmaps/medical.public.md",  # see B11
}
