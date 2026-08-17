"""Apple Notes, via the OS's own automation bridge. Phase 7.

JXA (`osascript -l JavaScript`) rather than parsing NoteStore.sqlite: the database
route (docs/12 §8) reads a reverse-engineered gzip+protobuf format that moves with
the OS, while the automation bridge is the interface Apple maintains and permission-
gates. The trade is speed — JXA enumerates the library on every run — which at
personal scale is seconds, and the mtime watermark means unchanged notes still cost
zero writes downstream.

First run prompts for Automation permission (System Settings → Privacy & Security →
Automation → allow the terminal to control Notes). A denial surfaces in health(),
not in a log line.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from backglass.connectors.base import Cursor, Health, SourceItem, content_hash
from backglass.connectors.boundary import Boundary

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

#: Emits one JSON array of {id, name, body, modified, created, folder}. `plaintext`
#: spares an HTML-stripping pass and is what the extraction model should read anyway.
_SCRIPT = """
ObjC.import('stdlib');
const app = Application('Notes');
const out = [];
const notes = app.notes();
for (let i = 0; i < notes.length; i++) {
  const n = notes[i];
  out.push({
    id: n.id(),
    name: n.name(),
    body: n.plaintext(),
    modified: n.modificationDate().toISOString(),
    created: n.creationDate().toISOString(),
    folder: (() => { try { return n.container().name(); } catch (e) { return ''; } })(),
  });
}
JSON.stringify(out);
"""


def run_osascript(script: str) -> str:
    done = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip() or "osascript failed")
    return done.stdout.strip()


@dataclass(kw_only=True)
class AppleNotesConnector:
    boundary: Boundary
    #: Injected so tests never touch osascript, mirroring the transport seam the API
    #: connectors use.
    runner: Callable[[str], str] = run_osascript

    cursor: Cursor = None
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return "apple-notes"

    def health(self) -> Health:
        try:
            self.runner("Application('Notes').name();")
        except Exception as exc:  # noqa: BLE001 — every failure is a product state here
            return Health(
                name=self.name,
                ok=False,
                detail=(
                    f"Notes automation unavailable: {exc}. Grant Automation access in "
                    "System Settings → Privacy & Security → Automation."
                ),
            )
        return Health(name=self.name, ok=True)

    def upstream_count(self) -> int | None:
        """How many notes this connector would ingest if it started from nothing.

        Compared against the ledger's row count, this answers the liveness question a
        days-since-last-row threshold can only guess at — see `base.Countable`. Notes is
        the store that motivated it: seventeen days quiet, two checks calling the cursor
        parked, and 65 against 65 when someone finally looked.

        Deliberately mirrors `fetch`'s filter rather than counting raw notes: a
        boundary-excluded note is not stored, so counting it would report a permanent
        one-item gap on a healthy source and re-create the false alarm in a new place.
        The `since` watermark is *not* applied — the question is what the store holds in
        total, not what is new.
        """
        try:
            notes = json.loads(self.runner(_SCRIPT) or "[]")
        except Exception:  # noqa: BLE001 — unknown is a real answer; see the protocol
            return None
        return sum(
            1
            for note in notes
            if self.boundary.check(_EMAIL.findall(str(note.get("body") or ""))).allowed
        )

    def fetch(self, since: Cursor) -> Iterator[SourceItem]:
        notes = json.loads(self.runner(_SCRIPT) or "[]")
        watermark = since or ""
        newest = watermark
        # Sorted so the watermark only ever moves forward past yielded items.
        for note in sorted(notes, key=lambda n: str(n.get("modified") or "")):
            modified = str(note.get("modified") or "")
            if watermark and modified <= watermark:
                continue
            newest = max(newest, modified)
            body = str(note.get("body") or "")
            verdict = self.boundary.check(_EMAIL.findall(body))
            if not verdict.allowed:
                self.excluded += 1
                rule = verdict.matched_rule or "boundary"
                self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + 1
                continue
            title = str(note.get("name") or "untitled")
            yield SourceItem(
                source=self.name,
                external_id=str(note["id"]),
                occurred_at=modified,
                author=None,
                title=title,
                body_text=body[:20000],
                raw_json=json.dumps(
                    {"folder": note.get("folder"), "created": note.get("created")}
                ),
                content_hash=content_hash(
                    author=None, title=title, body_text=body[:20000], occurred_at=modified
                ),
            )
        self.cursor = newest or since
