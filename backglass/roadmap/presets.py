"""Loads roadmap presets from specs/roadmaps/*.md at runtime.

Same contract as extract/prompts.py: frontmatter carries the version, the file is read at
runtime, and the version is stamped on every row instantiation produces
(`roadmap.path_version`). A preset edited after instantiation therefore never silently
rewrites a roadmap the owner has already been living with — the stamp says which text it
came from.

Steps keep file order. Offsets are deliberately *not* required to be monotonic: a preset
may legitimately run two tracks in parallel (study while applying), and sorting by date
would reorder them behind the author's back.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backglass.config import REPO_ROOT

PRESETS_DIR = REPO_ROOT / "specs" / "roadmaps"

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_STEPS_BLOCK = re.compile(r"^##\s+Steps\s*$\n+^```\w*\n(.*?)^```", re.DOTALL | re.MULTILINE)

_REQUIRED_META = ("id", "version", "title", "horizon", "definition_of_done")
_HORIZONS = ("annual", "quarterly")


class PresetError(RuntimeError):
    pass


@dataclass(frozen=True)
class PresetStep:
    key: str
    title: str
    offset_weeks: int
    detail: str | None = None
    cadences: tuple[str, ...] = ()


@dataclass(frozen=True)
class PresetCadence:
    key: str
    title: str
    weekly_count: int
    estimated_minutes_each: int


@dataclass(frozen=True)
class PresetTotal:
    """A lifetime accumulator target (Phase 10): reach total_count, log against it."""

    key: str
    title: str
    total_count: int


@dataclass(frozen=True)
class Preset:
    id: str
    version: str
    title: str
    horizon: str
    definition_of_done: str
    steps: list[PresetStep]
    cadences: list[PresetCadence]
    totals: list[PresetTotal]
    path: Path
    #: Optional `signals:` frontmatter — comma-separated words that, appearing across
    #: several open commitments, suggest the owner is already living this path
    #: without tracking it. Empty means the preset never volunteers itself; it can
    #: still be started deliberately.
    signals: tuple[str, ...] = ()

    @property
    def stamp(self) -> str:
        """What `roadmap.path_version` holds alongside `path_id`."""
        return f"{self.id}@{self.version}"

    def cadence(self, key: str) -> PresetCadence | None:
        return next((c for c in self.cadences if c.key == key), None)


def load(path_id: str, base_dir: Path | None = None) -> Preset:
    # `path_id` is a URL segment (/roadmaps/start/{path_id}), so the join is a traversal
    # primitive until it is contained: `../../../etc/passwd` reads a file this parser
    # then quotes back in its own error messages. Resolve both sides — a `..` inside the
    # join and a symlink out of the tree both survive a string comparison.
    root = (base_dir or PRESETS_DIR).resolve()
    path = (root / f"{path_id}.md").resolve()
    # One message for "outside the tree" and "not there", naming only what the caller
    # sent. Distinguishing them answers "does this file exist?" for arbitrary paths, and
    # printing the resolved path maps the filesystem into a 422 body.
    if not path.is_relative_to(root) or not path.is_file():
        raise PresetError(f"no roadmap preset {path_id!r}")
    raw = path.read_text()

    meta = _frontmatter(path, raw)
    body = _steps_block(path, raw)

    steps = [_step(path, item) for item in _list(path, body, "steps")]
    cadences = [_cadence(path, item) for item in _list(path, body, "cadences")]
    totals = [_total(path, item) for item in _list(path, body, "totals")]

    _no_duplicates(path, "step", [s.key for s in steps])
    _no_duplicates(path, "cadence", [c.key for c in cadences])
    _no_duplicates(path, "total", [t.key for t in totals])

    known = {c.key for c in cadences}
    for step in steps:
        unknown = sorted(set(step.cadences) - known)
        if unknown:
            raise PresetError(
                f"{path.name}: step {step.key!r} references unknown cadence(s) {unknown}"
            )

    return Preset(
        id=meta["id"],
        version=meta["version"],
        title=meta["title"],
        horizon=meta["horizon"],
        definition_of_done=meta["definition_of_done"],
        steps=steps,
        cadences=cadences,
        totals=totals,
        path=path,
        signals=tuple(
            w.strip().lower() for w in meta.get("signals", "").split(",") if w.strip()
        ),
    )


def list_paths(base_dir: Path | None = None) -> list[Preset]:
    directory = base_dir or PRESETS_DIR
    return [load(path.stem, directory) for path in sorted(directory.glob("*.md"))]


# ─────────────────────────────────────────────────────────────── parsing


def _frontmatter(path: Path, raw: str) -> dict[str, str]:
    front = _FRONTMATTER.match(raw)
    if not front:
        raise PresetError(f"{path.name} has no frontmatter block")
    meta: dict[str, str] = {}
    for line in front.group(1).splitlines():
        key, _, value = line.partition(":")
        if key.strip():
            meta[key.strip()] = value.strip()
    for required in _REQUIRED_META:
        if not meta.get(required):
            raise PresetError(f"{path.name} frontmatter is missing {required!r}")
    if meta["horizon"] not in _HORIZONS:
        raise PresetError(
            f"{path.name}: horizon must be one of {_HORIZONS}, got {meta['horizon']!r}"
        )
    return meta


def _steps_block(path: Path, raw: str) -> dict[str, Any]:
    block = _STEPS_BLOCK.search(raw)
    if not block:
        raise PresetError(f"{path.name} has no fenced JSON block under a '## Steps' heading")
    try:
        parsed = json.loads(block.group(1))
    except json.JSONDecodeError as exc:
        raise PresetError(f"{path.name}: '## Steps' block is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PresetError(f"{path.name}: '## Steps' block must be a JSON object")
    return parsed


def _list(path: Path, body: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = body.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise PresetError(f"{path.name}: {key!r} must be a list of objects")
    items: list[dict[str, Any]] = value
    if key == "steps" and not items:
        raise PresetError(f"{path.name}: a preset with no steps is not a roadmap")
    return items


def _step(path: Path, item: dict[str, Any]) -> PresetStep:
    for required in ("key", "title", "offset_weeks"):
        if required not in item:
            raise PresetError(f"{path.name}: a step is missing {required!r}")
    cadences = item.get("cadences", [])
    if not isinstance(cadences, list):
        raise PresetError(f"{path.name}: step {item['key']!r} has a non-list 'cadences'")
    return PresetStep(
        key=str(item["key"]),
        title=str(item["title"]),
        offset_weeks=int(item["offset_weeks"]),
        detail=str(item["detail"]) if item.get("detail") else None,
        cadences=tuple(str(c) for c in cadences),
    )


def _cadence(path: Path, item: dict[str, Any]) -> PresetCadence:
    for required in ("key", "title", "weekly_count", "estimated_minutes_each"):
        if required not in item:
            raise PresetError(f"{path.name}: a cadence is missing {required!r}")
    return PresetCadence(
        key=str(item["key"]),
        title=str(item["title"]),
        weekly_count=int(item["weekly_count"]),
        estimated_minutes_each=int(item["estimated_minutes_each"]),
    )


def _total(path: Path, item: dict[str, Any]) -> PresetTotal:
    for required in ("key", "title", "total_count"):
        if required not in item:
            raise PresetError(f"{path.name}: a total is missing {required!r}")
    count = int(item["total_count"])
    if count <= 0:
        raise PresetError(f"{path.name}: total {item['key']!r} needs a positive total_count")
    return PresetTotal(key=str(item["key"]), title=str(item["title"]), total_count=count)


def _no_duplicates(path: Path, what: str, keys: list[str]) -> None:
    """Duplicate keys would collide on roadmap_step's UNIQUE(roadmap_id, step_key) halfway
    through instantiation, leaving a half-built roadmap and an opaque IntegrityError."""
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            raise PresetError(f"{path.name}: duplicate {what} key {key!r}")
        seen.add(key)
