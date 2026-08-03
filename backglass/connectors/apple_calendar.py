"""Calendar.app, via the OS's own automation bridge.

The point of this connector is that it needs nothing from anybody. Calendar.app already
holds whatever accounts macOS has been told about — on this machine that is both of the
owner's Google calendars — so the events are on disk and readable *now*, with no Google
Cloud project, no OAuth client, no consent flow, and no Full Disk Access. The Google
connector in `calendar.py` remains the right answer for a machine where Calendar.app is
not configured, or where the owner wants an account macOS does not have; this is the one
that works today.

Same bridge as `apple_notes.py` and `reminders.py`, and the same permission: Automation,
which this machine has already granted. Not the `~/Library/Calendars` store, which is
Full-Disk-Access-gated and an undocumented format.

**The source name is `calendar:apple` on purpose.** `plan/capacity.py::fixed_events`
selects `source LIKE 'calendar%'` and reads the event fields out of `raw_json`, so
matching that contract means these events subtract from the day's capacity the moment
they land, with no change to the planner. The `raw_json` shape below is copied from
`calendar.py::_to_item` for exactly that reason — if one changes, both must.

Windowed rather than incremental. There is no modification watermark available through
the bridge, so each run re-reads a fixed window and leans on `content_hash` for
idempotency: an unchanged event is recognised in `upsert_source_item` and writes nothing.
That is a deliberate exception to the cursor rule in `docs/07`, which exists to stop a
connector re-fetching *everything* forever; here the window is bounded and small.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from backglass.connectors.base import Cursor, Health, SourceItem, content_hash
from backglass.connectors.boundary import Boundary

#: Matching `calendar.py`'s window: enough history for "what did I do last week" and
#: enough future for the day planner's horizon, without enumerating a decade.
LOOKBACK_DAYS = 7
HORIZON_DAYS = 21

#: Two scripts, not one, and that split is the fix for a real failure. Asking for every
#: calendar's events in a single call is one long Apple Event, and macOS enforces its own
#: ~2-minute ceiling on those regardless of the subprocess timeout below — the owner's
#: machine returned `AppleEvent timed out (-1712)` and marked the whole source failed.
#: Naming the calendars first costs one cheap call and lets each query be small.
_CALENDARS_SCRIPT = 'JSON.stringify(Application("Calendar").calendars().map(c => c.name()));'

#: One calendar, one window. `%(name)s` is JSON-encoded by the caller, so a calendar
#: called `O'Brien" family` cannot terminate the string and change the script.
_EVENTS_SCRIPT = """
const cal = Application("Calendar");
const from = new Date(%(from_ms)d);
const to = new Date(%(to_ms)d);
const wanted = %(name)s;
const out = [];
for (const c of cal.calendars.whose({name: wanted})()) {
  let events;
  try {
    events = c.events.whose({_and: [{startDate: {">": from}}, {startDate: {"<": to}}]})();
  } catch (e) { continue; }
  for (const e of events) {
    try {
      out.push({
        calendar: c.name(),
        uid: e.uid(),
        title: e.summary(),
        starts_at: e.startDate().toISOString(),
        ends_at: e.endDate().toISOString(),
        all_day: e.alldayEvent(),
        location: e.location(),
        status: (function () {
          try { return String(e.status()); } catch (err) { return ""; }
        })(),
      });
    } catch (err) { /* one unreadable event must not lose the rest of the calendar */ }
  }
}
JSON.stringify(out);
"""

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

#: docs/07 §Calendar: "Declined events are excluded from capacity." The bridge exposes an
#: event's own status, not the owner's participation, so a cancelled event is dropped and
#: everything else counts — the conservative direction, matching `BUSY_STATUSES`.
_CANCELLED = "cancelled"

#: Same heuristic as the Google connector: an event that is travel occupies the day but
#: is reported separately, because "five hours of flights" is not "five hours of meetings".
_TRAVEL = re.compile(
    r"\b(flight|flying|drive to|driving to|train|commute|travel to|airport|depart)\b",
    re.IGNORECASE,
)


def run_osascript(script: str) -> str:
    done = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", script],
        capture_output=True,
        text=True,
        # Belt to macOS's own braces. The Apple Event ceiling (~2 min) is the one that
        # actually fires, and it is not configurable from here — which is why the work is
        # split per calendar above rather than made to wait longer. This timeout only
        # catches a wedged osascript, and reads as a failed source per rule 5.
        timeout=120,
    )
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip() or "osascript failed")
    return done.stdout.strip()


@dataclass(kw_only=True)
class AppleCalendarConnector:
    boundary: Boundary
    #: Injected so tests never touch osascript, mirroring every other local connector.
    runner: Callable[[str], str] = run_osascript
    #: Calendars to skip by name. Subscribed holiday and birthday feeds are noise that
    #: would otherwise consume the day planner's capacity every week.
    skip: tuple[str, ...] = ()
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    cursor: Cursor = None
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)
    #: Calendars that errored this run, reported rather than swallowed. Non-empty means
    #: the day plan is missing whatever was in them.
    failed_calendars: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "calendar:apple"

    def health(self) -> Health:
        try:
            self.runner('Application("Calendar").name();')
        except Exception as exc:  # noqa: BLE001 — every failure is a product state here
            return Health(
                name=self.name,
                ok=False,
                detail=(
                    f"Calendar automation unavailable: {exc}. Grant Automation access in "
                    "System Settings → Privacy & Security → Automation."
                ),
            )
        return Health(name=self.name, ok=True)

    def fetch(self, since: Cursor) -> Iterator[SourceItem]:
        del since  # windowed, not incremental — see the module docstring
        self.excluded = 0
        self.excluded_by_rule = {}
        self.failed_calendars = []

        now = self.now()
        window = {
            "from_ms": int((now - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000),
            "to_ms": int((now + timedelta(days=HORIZON_DAYS)).timestamp() * 1000),
        }
        skip = {name.casefold() for name in self.skip}

        # Skipped calendars are dropped before they are ever queried. That is not only
        # tidiness: each query is an Apple Event, and not spending one on a subscribed
        # holiday feed is most of what keeps the run inside the OS's ceiling.
        names = [
            name
            for name in json.loads(self.runner(_CALENDARS_SCRIPT) or "[]")
            if str(name).casefold() not in skip
        ]

        events: list[dict[str, object]] = []
        for name in names:
            script = _EVENTS_SCRIPT % {**window, "name": json.dumps(str(name))}
            try:
                events.extend(json.loads(self.runner(script) or "[]"))
            except Exception as exc:  # noqa: BLE001 — rule 5: one calendar is not the source
                # A calendar that times out or errors costs its own events and nothing
                # else. Losing one shared feed used to fail the whole connector and mark
                # the credential dead, which took the day plan's real meetings with it.
                self.excluded_by_rule[f"calendar:{name}"] = 1
                self.failed_calendars.append(f"{name}: {exc}")

        # Deduplicated across calendars, which is not a nicety. On the owner's machine
        # the same class sits in both a local "Work" calendar and a local "Family" one
        # with two different UIDs, so three of nine events were doubles — and the planner
        # would have subtracted each lecture from the day twice, silently halving a
        # teaching day. Identity is (title, start, end): a genuinely distinct event does
        # not share all three, and the same meeting synced twice always does.
        #
        # The winner is chosen by sorting rather than by arrival order, so the same UID
        # wins on every run and the ledger does not churn between two spellings of one
        # event. `_to_item` runs after the choice, so a skipped or boundary-excluded
        # calendar cannot claim a slot and suppress its twin.
        kept: dict[tuple[str, str, str], dict[str, object]] = {}
        for event in sorted(
            events, key=lambda e: (str(e.get("calendar") or ""), str(e.get("uid") or ""))
        ):
            if str(event.get("calendar") or "").casefold() in skip:
                continue
            identity = (
                str(event.get("title") or ""),
                str(event.get("starts_at") or ""),
                str(event.get("ends_at") or ""),
            )
            kept.setdefault(identity, event)

        for event in kept.values():
            item = self._to_item(event, skip)
            if item is not None:
                yield item
        self.cursor = now.isoformat()

    def _to_item(self, event: dict[str, object], skip: set[str]) -> SourceItem | None:
        calendar = str(event.get("calendar") or "")
        if calendar.casefold() in skip:
            return None
        if str(event.get("status") or "").casefold() == _CANCELLED:
            return None
        if event.get("all_day"):
            # docs/07: an all-day event does not consume a working window the way a
            # meeting does, so it is not capacity. Same call the Google connector makes.
            return None

        starts_at = str(event.get("starts_at") or "")
        ends_at = str(event.get("ends_at") or "")
        uid = str(event.get("uid") or "")
        if not starts_at or not ends_at or not uid:
            return None

        title = str(event.get("title") or "Busy")
        location = str(event.get("location") or "")

        # The boundary runs before persistence, as in every other connector: a title or
        # location naming a client address makes this client correspondence.
        verdict = self.boundary.check(_EMAIL.findall(f"{title} {location}"))
        if not verdict.allowed:
            self.excluded += 1
            rule = verdict.matched_rule or "boundary"
            self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + 1
            return None

        payload = {
            "starts_at": starts_at,
            "ends_at": ends_at,
            "status": "confirmed",
            "declined": False,
            "travel": bool(_TRAVEL.search(title)),
            "calendar": calendar,
            "location": location or None,
        }
        return SourceItem(
            source=self.name,
            # The event's own UID, which is stable across edits and across the accounts
            # Calendar.app syncs — so an edited meeting updates in place instead of
            # arriving as a second event.
            external_id=uid,
            occurred_at=starts_at,
            author=None,
            title=title,
            # No body: docs/07 says the calendar is handled by the connector rather than
            # by extraction, so there is nothing here for the model to read.
            body_text=None,
            raw_json=json.dumps(payload),
            content_hash=content_hash(
                author=None, title=title, body_text=json.dumps(payload), occurred_at=starts_at
            ),
        )
