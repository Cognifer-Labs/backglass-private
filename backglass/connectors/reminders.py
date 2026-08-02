"""Apple Reminders, via the OS automation bridge. Phase 7.

A reminder is the owner's own words about an obligation — the closest thing to a
pre-extracted commitment any source offers. It still lands as an ordinary
source_item and flows through triage/extraction like everything else, because the
ledger has one write path, not two (docs/02).

JXA over EventKit/pyobjc: zero dependencies, and the permission surface is the one
the OS already manages (docs/12 §8 recommends exactly this start). Reminders'
scripting bridge exposes no modification date, so the cursor is a fetch-window
watermark: each run reads everything incomplete plus anything completed since the
last run, and content_hash makes the re-read of unchanged rows write-free — the
idempotency cost model, applied where a real cursor does not exist.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from backglass.connectors.apple_notes import run_osascript
from backglass.connectors.base import Cursor, Health, SourceItem, content_hash
from backglass.connectors.boundary import Boundary

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

#: {since} is interpolated as an ISO string ('' on first run). Incomplete reminders
#: always; completed ones only when they completed after the watermark.
_SCRIPT_TEMPLATE = """
const app = Application('Reminders');
const since = {since_js};
const out = [];
const lists = app.lists();
for (let i = 0; i < lists.length; i++) {{
  const rs = lists[i].reminders();
  for (let j = 0; j < rs.length; j++) {{
    const r = rs[j];
    const completed = r.completed();
    let completionDate = null;
    if (completed) {{
      const cd = r.completionDate();
      if (!cd) continue;
      completionDate = cd.toISOString();
      if (since && completionDate <= since) continue;
    }}
    const due = r.dueDate();
    out.push({{
      id: r.id(),
      name: r.name(),
      body: r.body() || '',
      list: lists[i].name(),
      completed: completed,
      completionDate: completionDate,
      dueDate: due ? due.toISOString() : null,
      creationDate: r.creationDate().toISOString(),
    }});
  }}
}}
JSON.stringify(out);
"""


@dataclass(kw_only=True)
class RemindersConnector:
    boundary: Boundary
    runner: Callable[[str], str] = run_osascript

    cursor: Cursor = None
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return "reminders"

    def health(self) -> Health:
        try:
            self.runner("Application('Reminders').name();")
        except Exception as exc:  # noqa: BLE001 — every failure is a product state here
            return Health(
                name=self.name,
                ok=False,
                detail=(
                    f"Reminders automation unavailable: {exc}. Grant Automation access "
                    "in System Settings → Privacy & Security → Automation."
                ),
            )
        return Health(name=self.name, ok=True)

    def fetch(self, since: Cursor) -> Iterator[SourceItem]:
        script = _SCRIPT_TEMPLATE.format(since_js=json.dumps(since or ""))
        reminders = json.loads(self.runner(script) or "[]")
        watermark = str(since or "")
        newest = watermark
        for r in reminders:
            # The watermark is an upstream timestamp, never the wall clock. A clock
            # reading taken after the snapshot covers a window this run never saw, and
            # the JXA filter above then skips anything completed inside it — forever,
            # because a completion has one timestamp and no second chance. Same rule as
            # apple_notes/files/notes: advance only past what was actually observed,
            # including boundary-excluded rows, which were seen even though they are
            # not stored.
            observed = str(r.get("completionDate") or r.get("creationDate") or "")
            newest = max(newest, observed)

            text_parts = [str(r.get("name") or "")]
            if r.get("body"):
                text_parts.append(str(r["body"]))
            if r.get("dueDate"):
                text_parts.append(f"due {r['dueDate']}")
            if r.get("completed"):
                text_parts.append(f"completed {r.get('completionDate')}")
            body = "\n".join(text_parts)

            verdict = self.boundary.check(_EMAIL.findall(body))
            if not verdict.allowed:
                self.excluded += 1
                rule = verdict.matched_rule or "boundary"
                self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + 1
                continue

            # source_item is immutable (0002), so a completion cannot rewrite the
            # original row — it becomes its own item with a derived external_id, an
            # event the extractor can supersede the open commitment against. The
            # completion timestamp is part of that id because a reminder can be
            # completed, un-completed and completed again: without it both completions
            # claim one external_id with different content, which upsert_source_item
            # reads as an immutability conflict and resolves by keeping the first —
            # the second completion is dropped and nothing anywhere says so.
            occurred = str(r.get("completionDate") or r.get("creationDate"))
            title = str(r.get("name") or "untitled")
            external_id = str(r["id"])
            if r.get("completed"):
                external_id = f"{external_id}:completed:{r.get('completionDate')}"
            yield SourceItem(
                source=self.name,
                external_id=external_id,
                occurred_at=occurred,
                author="me",
                title=title,
                body_text=body[:20000],
                raw_json=json.dumps(
                    {
                        "list": r.get("list"),
                        "completed": r.get("completed"),
                        "dueDate": r.get("dueDate"),
                    }
                ),
                content_hash=content_hash(
                    author="me", title=title, body_text=body[:20000], occurred_at=occurred
                ),
            )
        # Window watermark, not a change cursor — see module docstring. Held rather
        # than reset when a run observes nothing.
        self.cursor = newest or since
