"""Assemble, scrub, test, and commit the public export tree.

Never uses `git clone`/`git archive`/`git filter-repo` on the private repo — a
plain file copy into a directory with no `.git` at any point, so contaminated
history can never ride along even if file contents are clean. That is a hard
constraint on this file, not a preference (see the plan's Risks section).

Usage:
    uv run python scripts/release/build_public_repo.py --dry-run
    uv run python scripts/release/build_public_repo.py \
        --out /private/tmp/backglass-release/2026-08-02
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from manifest import ALLOW_PATHS, DENY_PATHS, SUBSTITUTIONS  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

COMMIT_MESSAGE = """Backglass: a commitment ledger, not a search tool over your inbox

Reads email, documents, and notes; extracts typed commitments, deadlines, and
schedule blocks at ingest; reports over that ledger via a morning brief and a
local dashboard. SQLite, server-rendered HTML, bring-your-own model key
(Anthropic, DeepInfra, or the Claude Code CLI) — no hosted service, everything
runs on your machine.

See README.md for the product framing and GETTING_STARTED.md for setup.

MIT licensed, see LICENSE.
"""


def _matches_prefix(rel_path: str, prefixes: list[str]) -> bool:
    for prefix in prefixes:
        if prefix.endswith("/"):
            if rel_path.startswith(prefix):
                return True
        elif rel_path == prefix:
            return True
    return False


def resolve_export_files(repo_root: Path) -> dict[str, str]:
    """Return {dest_relpath: src_relpath} for every file that ships.

    `git ls-files` filtered to ALLOW_PATHS minus DENY_PATHS, then
    SUBSTITUTIONS entries are added explicitly — their destination path is
    allowed to ship even though the *original* content at that path (e.g.
    `specs/roadmaps/medical.md`) is denylisted, because the substitution
    supplies different, already-vetted source content instead.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = result.stdout.splitlines()

    export: dict[str, str] = {}
    for rel_path in tracked:
        if not _matches_prefix(rel_path, ALLOW_PATHS):
            continue
        if _matches_prefix(rel_path, DENY_PATHS):
            continue
        export[rel_path] = rel_path

    for dest_rel, src_rel in SUBSTITUTIONS.items():
        export[dest_rel] = src_rel

    return export


def copy_tree(repo_root: Path, out_dir: Path, export: dict[str, str]) -> None:
    for dest_rel, src_rel in sorted(export.items()):
        src = repo_root / src_rel
        dest = out_dir / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def run(cmd: list[str], cwd: Path) -> None:
    print(f"$ {' '.join(cmd)}  (cwd={cwd})")
    proc = subprocess.run(cmd, cwd=cwd)
    if proc.returncode != 0:
        print(f"error: `{' '.join(cmd)}` exited {proc.returncode}", file=sys.stderr)
        raise SystemExit(proc.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Scratch directory to assemble the export into. Required unless --dry-run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the file list that would be copied; write nothing.",
    )
    args = parser.parse_args()

    export = resolve_export_files(REPO_ROOT)

    if args.dry_run:
        for dest_rel in sorted(export):
            print(dest_rel)
        print(f"\n{len(export)} files would be exported.", file=sys.stderr)
        return 0

    if args.out is None:
        parser.error("--out is required unless --dry-run")

    out_dir: Path = args.out.resolve()
    if str(out_dir).startswith(str(REPO_ROOT)):
        print(
            f"error: --out ({out_dir}) must be outside the private checkout ({REPO_ROOT})",
            file=sys.stderr,
        )
        return 2

    if out_dir.exists():
        print(f"error: {out_dir} already exists; refusing to overwrite", file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True)
    copy_tree(REPO_ROOT, out_dir, export)
    print(f"copied {len(export)} files to {out_dir}")

    # Hard guard: never proceed to `git init` if a `.git` directory somehow
    # made it into the scratch tree (e.g. a future edit accidentally
    # reintroducing a clone-based copy step).
    if (out_dir / ".git").exists():
        print(f"error: {out_dir}/.git already exists before git init", file=sys.stderr)
        return 2

    scrub_gate = Path(__file__).resolve().parent / "scrub_gate.py"
    run([sys.executable, str(scrub_gate), str(out_dir)], cwd=REPO_ROOT)

    run(["uv", "sync"], cwd=out_dir)
    run(["uv", "run", "pytest"], cwd=out_dir)

    run(["git", "init"], cwd=out_dir)
    run(["git", "add", "-A"], cwd=out_dir)
    # No -c user.name/user.email override here: the scratch dir has no local git
    # config of its own, so `git commit` falls through to whatever identity is
    # configured (global or system) on the machine running this script — the
    # repo's own default identity on this machine, and the right identity for
    # any future fork running its own release from its own global config.
    identity = subprocess.run(
        ["git", "config", "--get", "user.email"], cwd=out_dir, capture_output=True, text=True
    )
    if identity.returncode != 0 or not identity.stdout.strip():
        print(
            "error: no git identity configured (user.name/user.email) — "
            "set it globally before running this script",
            file=sys.stderr,
        )
        return 2
    run(["git", "commit", "-m", COMMIT_MESSAGE], cwd=out_dir)

    rev_count = subprocess.run(
        ["git", "-C", str(out_dir), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if rev_count != "1":
        print(f"error: expected exactly 1 commit, found {rev_count}", file=sys.stderr)
        return 2

    print(f"\nbuilt public export at {out_dir} — single commit confirmed.")
    print("Not pushed. Push is a separate, explicit Phase E step.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
