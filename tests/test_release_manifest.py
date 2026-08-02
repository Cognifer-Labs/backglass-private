"""Regression test for item H: the public-export boundary in
`scripts/release/manifest.py` stays correctly closed as the manifest is edited.

Static assertions against the manifest's data structures only — no filesystem
walk of the tracked tree and no `build_public_repo.py` invocation (that's
covered by Phase D's export run, which is slow and touches the network via
`uv sync`). These checks are cheap and permanent: they belong in the regular
suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "release"))

from manifest import ALLOW_PATHS, DENY_PATHS, SUBSTITUTIONS  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent


def _reachable_under_allow(path: str) -> bool:
    """True if `path` would match any ALLOW_PATHS prefix (before any deny check)."""
    for prefix in ALLOW_PATHS:
        if prefix.endswith("/"):
            if path.startswith(prefix):
                return True
        elif path == prefix:
            return True
    return False


def test_deny_paths_not_reachable_under_allow_prefixes():
    """A DENY_PATHS entry must not be silently un-denied by a broad ALLOW prefix.

    Concretely: docs/13 and docs/14 are denied *because* the docs/ allowlist is
    individual files, not a directory prefix. If a future edit collapses those
    individual `docs/0X-*.md` entries into a broad `"docs/"` prefix (as `specs/
    roadmaps/` already is), this test fails immediately — it does not wait for
    someone to notice the leak in a built export tree.

    Paths that participate in SUBSTITUTIONS (either side) are the one
    deliberate exception: `specs/roadmaps/medical.md` (a SUBSTITUTIONS key) is
    *meant* to be reachable under the broad `specs/roadmaps/` allow prefix —
    that reachability is exactly what lets the substituted content land at the
    real path. Its source, `specs/roadmaps/medical.public.md` (a SUBSTITUTIONS
    value), is intentionally denied as a standalone file per B11's
    "private-tree-only staging path" — it is also reachable under the same
    broad prefix, so it needs the same exemption for a different reason.
    """
    substitution_paths = set(SUBSTITUTIONS.keys()) | set(SUBSTITUTIONS.values())
    unexpectedly_reachable = [
        path
        for path in DENY_PATHS
        if path not in substitution_paths and _reachable_under_allow(path)
    ]
    assert unexpectedly_reachable == [], (
        f"DENY_PATHS entries reachable under an ALLOW_PATHS prefix without a "
        f"SUBSTITUTIONS exemption: {unexpectedly_reachable}"
    )


def test_substitution_keys_are_denied():
    """Every SUBSTITUTIONS destination must be denied at its real path.

    Otherwise the build script's "substitution wins over deny" behavior would
    be masking a leak of the real (denied) content rather than replacing it —
    e.g. if `specs/roadmaps/medical.md` were ever removed from DENY_PATHS, the
    real private roadmap would ship unsubstituted from the normal allow walk.
    """
    for dest_path in SUBSTITUTIONS:
        assert dest_path in DENY_PATHS, (
            f"SUBSTITUTIONS destination {dest_path!r} must also be in DENY_PATHS "
            "so the un-substituted content is never reachable from the normal "
            "allow walk."
        )


def test_substitution_sources_exist_on_disk():
    """Catches `medical.public.md` (or any future substitution source) going
    stale, renamed, or deleted silently — a build run would otherwise fail
    late, inside the scrub/test pipeline, instead of at this cheap check."""
    for dest_path, src_path in SUBSTITUTIONS.items():
        full_path = REPO_ROOT / src_path
        assert full_path.is_file(), (
            f"SUBSTITUTIONS source {src_path!r} (for destination {dest_path!r}) "
            f"does not exist on disk at {full_path}"
        )


def test_no_overlap_between_denied_data_and_env_and_allow_of_same_name():
    """Sanity check on the manifest itself: `data/`, `.env`, `.claude/` are
    denied and must never appear verbatim in ALLOW_PATHS (defense against a
    careless future edit re-adding one of them as an allow entry)."""
    always_private = {"data/", ".env", ".claude/"}
    assert always_private.issubset(set(DENY_PATHS))
    assert always_private.isdisjoint(set(ALLOW_PATHS))
