"""Obsidian notes. docs/07 §Notes.

docs/07 leaves the notes app unresolved and recommends Obsidian:

    "Roughly a tenth the work of the alternatives ... markdown files on disk, watch the
    vault directory, no API, no auth."

That recommendation is taken. The consequence is that this is the only connector with no
network call and no credential — the cursor is a filesystem mtime, and "auth expired" is
not a state it can be in. It is also, per docs/10 §Deployment, "the one piece that would
need rethinking if the pipeline moves off the machine holding the vault", so it fails
loudly rather than silently when the vault is not there.

The boundary still runs. A note can quote an email, and docs/08 D1 says the check happens
before persistence regardless of which connector is doing the persisting.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from backglass.connectors.base import Cursor, Health, SourceItem, content_hash
from backglass.connectors.boundary import Boundary

#: Obsidian's own directories, plus the ones every vault accumulates.
SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", ".DS_Store"}

#: Anything larger than this is a pasted export or a log, not a note. docs/02's per-item
#: ceiling applies at extraction; this stops the file ever being read into memory.
MAX_BYTES = 512_000

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


@dataclass
class NotesConnector:
    """Watches a vault directory. No API, no auth, no rate limit."""

    vault_path: Path
    boundary: Boundary
    label: str = "obsidian"

    cursor: Cursor = None
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return f"notes:{self.label}"

    def health(self) -> Health:
        if not self.vault_path:
            return Health(name=self.name, ok=False, detail="OBSIDIAN_VAULT_PATH is not set")
        if not self.vault_path.is_dir():
            # docs/10: this is the connector that breaks if the pipeline moves off the
            # machine holding the vault. Saying so beats reporting zero notes.
            return Health(
                name=self.name,
                ok=False,
                detail=(
                    f"vault not found at {self.vault_path} — is this the machine holding it?"
                ),
            )
        return Health(name=self.name, ok=True)

    def fetch(self, since: Cursor) -> Iterator[SourceItem]:
        """Yield notes modified since the cursor.

        The cursor is the highest mtime seen, as an ISO timestamp. Coarser than a file
        watcher and entirely sufficient at a 30-minute cadence — docs/02: "Nothing here is
        real time. An hour of lag is acceptable everywhere."
        """
        self.excluded = 0
        self.excluded_by_rule = {}
        if not self.vault_path.is_dir():
            raise FileNotFoundError(f"vault not found at {self.vault_path}")

        watermark = _parse(since)
        highest = watermark

        for path in sorted(self.vault_path.rglob("*.md")):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size > MAX_BYTES:
                continue
            modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            if watermark is not None and modified <= watermark:
                continue
            highest = modified if highest is None else max(highest, modified)

            item = self._to_item(path, modified)
            if item is not None:
                yield item

        if highest is not None:
            # Full precision, microseconds included. Truncating to the second rounds the
            # watermark *down*, so a file modified at 12:00:00.5 is always "newer" than a
            # cursor of 12:00:00 and every note is re-read on every run — which defeats
            # the point of having a cursor at all. Nothing displays this value; it is an
            # opaque position, per docs/07.
            self.cursor = highest.isoformat()

    def _to_item(self, path: Path, modified: datetime) -> SourceItem | None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

        # D1. Before persistence. A note that pastes in a client thread is client
        # correspondence, and the addresses in it are what the denylist matches on.
        verdict = self.boundary.check(_EMAIL.findall(text))
        if not verdict.allowed:
            self.excluded += 1
            rule = verdict.matched_rule or "?"
            self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + 1
            return None

        body, meta = _strip_frontmatter(text)
        if not body.strip():
            return None

        relative = path.relative_to(self.vault_path).as_posix()
        occurred = _occurred_at(meta, modified)
        return SourceItem(
            source=self.name,
            # A stable id that survives the file being renamed is not available without an
            # index, so the path is the id and a rename reads as a new note. Obsidian
            # renames are rare and a duplicate note is a far cheaper error than a lost one.
            external_id=hashlib.sha256(relative.encode()).hexdigest()[:32],
            occurred_at=occurred,
            author=None,
            title=meta.get("title") or path.stem,
            body_text=body.strip(),
            raw_json=json.dumps({"path": relative, "frontmatter": meta}, sort_keys=True),
            content_hash=content_hash(
                author=None, title=path.stem, body_text=body, occurred_at=occurred
            ),
        )


def _strip_frontmatter(text: str) -> tuple[str, dict[str, str]]:
    match = _FRONTMATTER.match(text)
    if not match:
        return text, {}
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        if key.strip():
            meta[key.strip().lower()] = value.strip()
    return text[match.end() :], meta


def _occurred_at(meta: dict[str, str], modified: datetime) -> str:
    """Prefer a date the note claims for itself over its mtime.

    CLAUDE.md rule 4 resolves relative dates against `occurred_at`, so a daily note dated
    2026-07-10 must say so — otherwise "by Friday" in a note edited today resolves against
    today, which is exactly the bug rule 4 exists to prevent.
    """
    for key in ("date", "created", "day"):
        raw = meta.get(key)
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.replace(microsecond=0).isoformat()
    return modified.replace(microsecond=0).isoformat()


def _parse(cursor: Cursor) -> datetime | None:
    if not cursor:
        return None
    try:
        return datetime.fromisoformat(str(cursor).replace("Z", "+00:00"))
    except ValueError:
        return None
