"""The drop folder. A directory the owner saves documents into.

Meeting notes, a signed contract, a scanned letter someone emailed as a file — anything
that is evidence but arrives outside the connected accounts. One file becomes one
source_item, and the folder is the whole interface: no API, no auth, no credential row.

Shape is deliberately the notes connector's, because the problem is the same one —
local files, an mtime watermark for a cursor, a boundary check before persistence. The
two differences are:

  - the id is the relative path *as itself*, not a hash of it. A drop folder is something
    the owner looks at, so an id they can read and locate beats one they cannot.
  - the folder is heterogeneous, so unsupported files are counted rather than ignored.
    Dropping a .docx in and having nothing happen, with nothing said, is the failure
    docs/11 §8 calls dangerous.

**Only .txt and .md are read.** PDFs are deliberately not supported: nothing in
pyproject.toml can extract text from one. drive.py appears to handle PDFs, but what it
actually does is UTF-8-decode the downloaded bytes — that yields object streams and
binary noise for a real PDF, not text. Adding a PDF library is a dependency decision that
belongs to whoever owns docs/10, not to this connector. Until then a dropped .pdf is
counted under "unsupported" and the owner is told, which is the honest failure.

The boundary still runs. A dropped file can be a printed email thread, and docs/08 D1
puts the check before persistence regardless of which connector is persisting.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from backglass.connectors.base import Cursor, Health, SourceItem, content_hash
from backglass.connectors.boundary import Boundary

#: What can be read as text today. See the module docstring for why .pdf is not here.
SUPPORTED_SUFFIXES = {".txt", ".md"}

#: Anything larger is an export or a log dump, not a document. Same ceiling as notes.py;
#: it stops the file being read into memory at all.
MAX_BYTES = 512_000

#: docs/02's per-item ceiling. A 300-page contract is not extracted whole.
MAX_CHARS = 20_000

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


@dataclass(kw_only=True)
class FilesConnector:
    """Watches a drop folder. No API, no auth, no rate limit."""

    folder_path: Path
    boundary: Boundary

    cursor: Cursor = None
    #: docs/08 D5's tally: items the *boundary* dropped. Format exclusions are a different
    #: kind of event — nothing was withheld for privacy — so they are counted in
    #: excluded_by_rule under "unsupported" without inflating this number.
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return "files"

    def health(self) -> Health:
        if not self.folder_path:
            return Health(name=self.name, ok=False, detail="drop folder path is not set")
        if not self.folder_path.is_dir():
            return Health(
                name=self.name,
                ok=False,
                detail=(
                    f"drop folder not found at {self.folder_path} — "
                    "is this the machine holding it?"
                ),
            )
        return Health(name=self.name, ok=True)

    def fetch(self, since: Cursor) -> Iterator[SourceItem]:
        """Yield files saved or re-saved since the cursor.

        The cursor is the highest mtime seen. docs/02: "Nothing here is real time. An hour
        of lag is acceptable everywhere," so a watermark beats a file watcher.
        """
        self.excluded = 0
        self.excluded_by_rule = {}
        if not self.folder_path.is_dir():
            raise FileNotFoundError(f"drop folder not found at {self.folder_path}")

        watermark = _parse(since)
        highest = watermark

        for path in sorted(self.folder_path.rglob("*")):
            relative = path.relative_to(self.folder_path)
            # Hidden files and hidden folders. .DS_Store, an editor's .swp, a synced
            # .dropbox directory — none of them are documents the owner dropped.
            if any(part.startswith(".") for part in relative.parts):
                continue
            if not path.is_file():
                continue

            try:
                stat = path.stat()
            except OSError:
                continue

            modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            if watermark is not None and modified <= watermark:
                continue

            if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                self._count("unsupported")
                # Still advances the watermark: the file will never be readable, so
                # re-considering it every run only re-counts it.
                highest = modified if highest is None else max(highest, modified)
                continue
            if stat.st_size > MAX_BYTES:
                self._count("too_large")
                highest = modified if highest is None else max(highest, modified)
                continue

            highest = modified if highest is None else max(highest, modified)
            item = self._to_item(path, relative, modified)
            if item is not None:
                yield item

        if highest is not None:
            # Full precision, microseconds included — tasks/lessons.md, 2026-07-30. A
            # watermark truncated to the second is always *older* than a file modified at
            # 12:00:00.5, so every file is re-read on every run and content_hash hides it
            # by making the writes idempotent anyway. The cursor is opaque (docs/07);
            # nothing displays this value.
            self.cursor = highest.isoformat()

    def _count(self, rule: str) -> None:
        self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + 1

    def _to_item(self, path: Path, relative: Path, modified: datetime) -> SourceItem | None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

        # D1. Before persistence. A printed thread dropped into the folder is still
        # correspondence, and the addresses in it are what the denylist matches on.
        verdict = self.boundary.check(_EMAIL.findall(text))
        if not verdict.allowed:
            self.excluded += 1
            self._count(verdict.matched_rule or "?")
            return None

        body, meta = _strip_frontmatter(text) if path.suffix.lower() == ".md" else (text, {})
        body = body.strip()
        if not body:
            return None
        body = body[:MAX_CHARS]

        external_id = relative.as_posix()
        occurred = _occurred_at(meta, modified)
        title = path.name
        return SourceItem(
            source=self.name,
            external_id=external_id,
            occurred_at=occurred,
            author=None,
            title=title,
            body_text=body,
            raw_json=json.dumps(
                {"path": external_id, "suffix": path.suffix.lower(), "frontmatter": meta},
                sort_keys=True,
            ),
            content_hash=content_hash(
                author=None, title=title, body_text=body, occurred_at=occurred
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
    """When the document happened.

    A dropped file's mtime is when the owner saved it, which is the only honest anchor
    available for a .txt. A markdown file that dates itself is believed instead — same
    rule as notes.py, and the reason is CLAUDE.md rule 4: relative dates resolve against
    occurred_at, so a note dated the 10th must not have "by Friday" resolved against the
    day it was dropped in.
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
