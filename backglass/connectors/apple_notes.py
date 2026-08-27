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
#:
#: **The watermark is applied here, in the script, and that is the whole performance
#: story.** Every property read over this bridge is its own Apple Event, so a note costs
#: six of them and the owner's 65 notes cost ~390 — which measured 2:02 on an idle machine
#: and over five minutes during a sync, so `apple-notes` timed out on every scheduled run
#: and collected nothing between 2026-07-05 and 2026-08-21. Almost all of that was waste:
#: the connector read every note's full body and then discarded all but the ones modified
#: since the watermark, which on a normal day is none of them.
#:
#: Reading `modificationDate` first and skipping costs one Apple Event per note instead of
#: six, so the steady-state run is ~65 round trips rather than ~390. The Python-side filter
#: in `fetch` stays as it was: it is cheap, and it keeps the connector correct if this
#: interpolation is ever wrong.
#:
#: `{since_js}` is JSON-encoded by the caller — the same discipline as `reminders` and as
#: `apple_calendar`'s calendar name — so a watermark can never terminate the string.
_SCRIPT_TEMPLATE = """
ObjC.import('stdlib');
const app = Application('Notes');
const since = {since_js};
const out = [];
const notes = app.notes();
for (let i = 0; i < notes.length; i++) {
  const n = notes[i];
  // One Apple Event. Everything below is five more, and is paid only for a note that
  // actually changed.
  const modified = n.modificationDate().toISOString();
  if (since && modified <= since) continue;
  out.push({
    id: n.id(),
    name: n.name(),
    body: n.plaintext(),
    modified: modified,
    created: n.creationDate().toISOString(),
    folder: (() => { try { return n.container().name(); } catch (e) { return ''; } })(),
  });
}
JSON.stringify(out);
"""


#: Seconds to wait on one Apple Events script. Measured, like `apple_calendar`'s: the
#: script above takes **2 minutes 2 seconds** to read the owner's 65 notes on an idle
#: machine, so at the previous value of 120 it failed by two seconds at rest and by more
#: whenever anything else was running — which is every scheduled sync, because the sync is
#: the thing running. `apple-notes` had therefore collected nothing since 2026-07-05 and
#: `reminders`, which shares this runner, nothing since 2026-08-14. Both reported the
#: failure honestly the whole time; nobody read `backglass doctor`.
#:
#: The cost is in the round trips, not the data: one note costs six Apple Events — `id`,
#: `name`, `plaintext`, two dates and the folder — so 65 notes is ~390 of them.
#:
#: Raising this number was the *first* fix and it was the wrong one, which is worth
#: recording rather than quietly overwriting. Notes still timed out at 300 seconds during a
#: sync, because the script was reading all 65 bodies and then discarding every note older
#: than the watermark — on a normal day, all of them. Applying the watermark inside the
#: script instead (see `_SCRIPT_TEMPLATE`) took the steady-state read from 2:02 to **0.9
#: seconds warm, 22 seconds cold**. This timeout is now headroom for `upstream_count`,
#: which deliberately reads everything, and for a first run against a large store — not a
#: number the common path is expected to approach.
OSASCRIPT_TIMEOUT_SECONDS = 300


def run_osascript(script: str, timeout: int = OSASCRIPT_TIMEOUT_SECONDS) -> str:
    done = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
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
            # No watermark: the whole point is the total the store holds, so this pays the
            # full ~6-Apple-Events-per-note cost that `fetch` now avoids. It runs only in
            # `doctor`, which is where the live probes already live.
            notes = json.loads(self.runner(self._script(None)) or "[]")
        except Exception:  # noqa: BLE001 — unknown is a real answer; see the protocol
            return None
        return sum(
            1
            for note in notes
            if self.boundary.check(_EMAIL.findall(str(note.get("body") or ""))).allowed
        )

    def _script(self, since: Cursor) -> str:
        """The reader, with the watermark baked in. JSON-encoded, so a stored cursor can
        never terminate the string and change the script."""
        return _SCRIPT_TEMPLATE.replace("{since_js}", json.dumps(str(since or "")))

    def fetch(self, since: Cursor) -> Iterator[SourceItem]:
        notes = json.loads(self.runner(self._script(since)) or "[]")
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
