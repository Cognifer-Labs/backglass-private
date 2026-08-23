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

**.txt, .md and .pdf are read.** PDF text comes from pypdf (BSD-3, pure Python, zero
transitive deps — the docs/12 §2 ruling; PyMuPDF is AGPL and 50 MB, pdfplumber is a table
tool we do not need). `pdf_text()` below is the shared extractor: drive.py routes
`application/pdf` downloads through it too, because before this it UTF-8-decoded the
bytes and stored object streams as if they were prose.

A scanned PDF has pages and no text. That is not a failure and not an empty file — it is
a document waiting for OCR — so it is stored with `"needs_ocr": true` in raw_json and
whatever few characters came out. OCR itself is a deliberate non-goal (docs/12 §2: an
OCR stack drags in tesseract and ghostscript); the flag is so the owner can find those
items later without a re-scan. An encrypted or corrupt PDF is genuinely unreadable and is
counted under `excluded_by_rule["unreadable"]`. Everything else — .docx, .pages — is
still counted under "unsupported" and the owner is told, which is the honest failure.

The boundary still runs. A dropped file can be a printed email thread, and docs/08 D1
puts the check before persistence regardless of which connector is persisting.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from backglass.connectors.base import Cursor, Health, SourceItem, content_hash
from backglass.connectors.boundary import Boundary

#: What can be read as text today.
SUPPORTED_SUFFIXES = {".txt", ".md", ".pdf"}

#: Anything larger is an export or a log dump, not a document. It stops the file being
#: read into memory at all, and `MAX_CHARS` below is what bounds extraction cost — so the
#: only thing this number protects is memory.
#:
#: Raised from notes.py's 512 KB on 2026-08-21, when the first real drop folder — a
#: semester of ASU coursework — had seven files over the old ceiling and every one of
#: them was a document: a 784 KB lab syllabus, a 2.8 MB recitation activity, a 2.3 MB
#: textbook chapter. They are large because they are typeset and full of figures, not
#: because they are dumps, and excluding a syllabus is exactly the failure docs/11 §8
#: calls dangerous. The text inside them is still cut to MAX_CHARS.
MAX_BYTES = 4_000_000

#: docs/02's per-item ceiling. A 300-page contract is not extracted whole.
MAX_CHARS = 20_000

#: Below this many characters out of a non-empty page count, the PDF is a picture of a
#: document rather than a document. docs/12 §2's "chars/page below a threshold" guard.
MIN_PDF_CHARS = 20

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


@dataclass(frozen=True)
class PdfText:
    """The result of reading a PDF. Three outcomes, not two.

    `readable=False` is an encrypted or malformed file: nothing can be said about it.
    `needs_ocr=True` is a scanned page: the document is real, the text is an image.
    """

    text: str
    pages: int
    readable: bool
    needs_ocr: bool


def pdf_text(data: bytes, *, max_chars: int = MAX_CHARS) -> PdfText:
    """Extract text from PDF bytes, page by page, stopping at the per-item ceiling.

    Shared by the drop folder and drive.py — a PDF is a PDF wherever it arrived from, and
    two extractors would drift. Never raises: a bad file is a `readable=False` result, so
    one unreadable document degrades rather than blocking the run (CLAUDE.md rule 5).
    """
    try:
        reader = PdfReader(BytesIO(data))
        # An encrypted PDF is unreadable, not empty. pypdf will happily hand back a reader
        # whose pages raise on access, so refuse it here rather than mid-loop. (An empty
        # user password would decrypt, but a document the owner locked is one they meant
        # to lock; guessing at it is not this connector's job.)
        if reader.is_encrypted:
            return PdfText(text="", pages=0, readable=False, needs_ocr=False)
        pages = len(reader.pages)
        parts: list[str] = []
        length = 0
        for page in reader.pages:
            try:
                chunk = page.extract_text() or ""
            except Exception:  # noqa: BLE001 - one bad page does not lose the others
                continue
            parts.append(chunk)
            length += len(chunk)
            if length >= max_chars:
                break
    except (PdfReadError, OSError, ValueError):
        return PdfText(text="", pages=0, readable=False, needs_ocr=False)

    text = "\n".join(parts).strip()[:max_chars]
    return PdfText(
        text=text,
        pages=pages,
        readable=True,
        needs_ocr=pages > 0 and len(text.strip()) < MIN_PDF_CHARS,
    )


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
        suffix = path.suffix.lower()
        needs_ocr = False
        if suffix == ".pdf":
            try:
                extracted = pdf_text(path.read_bytes())
            except OSError:
                return None
            if not extracted.readable:
                # Encrypted or malformed. Nothing was withheld for privacy, so this is a
                # rule count and not a boundary exclusion — same shape as "unsupported".
                self._count("unreadable")
                return None
            text = extracted.text
            needs_ocr = extracted.needs_ocr
        else:
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

        body, meta = _strip_frontmatter(text) if suffix == ".md" else (text, {})
        body = body.strip()
        # A scanned PDF is a real document with no extractable text. Dropping it would
        # lose the only record that it arrived; it is stored empty and flagged instead.
        if not body and not needs_ocr:
            return None
        body = body[:MAX_CHARS]

        external_id = relative.as_posix()
        occurred = _occurred_at(meta, modified)
        title = path.name
        raw: dict[str, Any] = {
            "path": external_id,
            "suffix": suffix,
            "frontmatter": meta,
        }
        if needs_ocr:
            raw["needs_ocr"] = True
        return SourceItem(
            source=self.name,
            external_id=external_id,
            occurred_at=occurred,
            author=None,
            title=title,
            body_text=body,
            raw_json=json.dumps(raw, sort_keys=True),
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
