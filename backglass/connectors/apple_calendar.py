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
from backglass.retraction import RetractableWindow

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
  // Deliberately NOT wrapped in try/catch. This query is the one that hits macOS's own
  // ~2-minute Apple Event ceiling and raises -1712, and swallowing it here returned `[]`
  // with exit code 0 — a calendar that timed out became a calendar with no events, with
  // nothing anywhere recording a failure. The isolation the catch was providing is
  // already provided: this script is invoked once per calendar, so an error costs its
  // own calendar and nothing else, and letting it propagate makes osascript exit
  // non-zero, which `run_osascript` turns into the RuntimeError that `failed_calendars`
  // is built from. `retractable_window` then correctly refuses to certify the read.
  const events = c.events.whose({_and: [{startDate: {">": from}}, {startDate: {"<": to}}]})();
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


#: Seconds to wait on one calendar's query. Measured, not guessed, and the measurement is
#: why it is this large: the owner's `dkesava2@asu.edu` calendar takes **80 seconds** to
#: return 28 days of events on an otherwise idle machine, and over **two minutes** while a
#: sync is doing anything else. At the previous value of 120 it therefore failed whenever
#: the machine was busy — which is every scheduled run, because the sync is what is busy.
#:
#: The cost of that was not a missing calendar. The connector caught the timeout per rule
#: 5, reported it as a boundary exclusion, and carried on returning **two events** for the
#: whole day: the planner then built a day around an almost-empty calendar. It also meant
#: `retractable_window` could never certify a read, so no cancelled class was ever
#: retracted — the fix built for that on 2026-08-20 could not fire once.
#:
#: Both ceilings are real and they fire in different regimes, which is why the comment
#: this replaces — asserting macOS's own ~2-minute Apple Event ceiling was "the one that
#: actually fires" — was half right and cost a fortnight. This subprocess limit is what
#: fires when the *script* runs long, and it is what produced the observed
#: `timed out after 120 seconds`. macOS's per-Apple-Event ceiling is separate, raises
#: -1712 inside osascript, and is why the `whose` query above is no longer wrapped in a
#: JXA try/catch: swallowed, it returned an empty calendar with exit code 0. The
#: standalone run that finished at 2:51 crossed neither, because no single Apple Event
#: in it ran for two minutes.
#:
#: Not solved by asking for less per event, which was tried first: reading each property
#: for the whole result set in one call (`spec.uid()`, `spec.summary()`, …) re-evaluates
#: the `whose` predicate per property and measured 2:51 against the current shape's 1:20.
#: The predicate is the cost, and it is paid once already.
OSASCRIPT_TIMEOUT_SECONDS = 300


def run_osascript(script: str, timeout: int = OSASCRIPT_TIMEOUT_SECONDS) -> str:
    done = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", script],
        capture_output=True,
        text=True,
        # Catches a wedged osascript, and reads as a failed source per rule 5. See the
        # constant above for why the number is what it is.
        timeout=timeout,
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
    #: The window this run actually read, and every external_id it returned inside it.
    #: `backglass.retraction` uses the pair to tell a cancelled class from a missing one.
    #: Reset at the top of every `fetch`, so a stale run cannot certify a new one.
    read_window: tuple[str, str] | None = None
    seen_ids: set[str] = field(default_factory=set)
    #: Calendars this run read all the way through. Certification is per calendar, not
    #: per run: the owner has one calendar that reliably takes minutes and sometimes
    #: fails, and an all-or-nothing rule let it veto retraction for every other calendar
    #: forever — which is how a cancelled class kept its slot in the day plan.
    complete_calendars: set[str] = field(default_factory=set)

    @property
    def name(self) -> str:
        return "calendar:apple"

    def retractable_window(self) -> RetractableWindow | None:
        """What this run is entitled to conclude from absence, and for which calendars.

        Scoped per calendar rather than per run, and that is the whole design. The owner
        has one calendar that takes minutes and intermittently fails; under an
        all-or-nothing rule it vetoed retraction for every *other* calendar on every run,
        so a cancelled class kept its slot in the day plan indefinitely. A calendar read
        all the way through is evidence about that calendar and about nothing else, which
        is exactly how it is used.

        A read that returned no events at all still certifies nothing. That is far more
        likely to be a broken bridge than a genuinely emptied calendar, and it costs only
        a real edge case: an owner who deletes every event in the window keeps stale rows
        until one new event lands. The failure it prevents is silent and total; the one it
        causes is visible and partial.
        """
        if self.read_window is None or not self.complete_calendars or not self.seen_ids:
            return None
        return RetractableWindow(
            starts_at=self.read_window[0],
            ends_before=self.read_window[1],
            seen_ids=set(self.seen_ids),
            calendars=set(self.complete_calendars),
        )

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
        self.read_window = None
        self.seen_ids = set()
        self.complete_calendars = set()

        now = self.now()
        starts_at = now - timedelta(days=LOOKBACK_DAYS)
        ends_before = now + timedelta(days=HORIZON_DAYS)
        window = {
            "from_ms": int(starts_at.timestamp() * 1000),
            "to_ms": int(ends_before.timestamp() * 1000),
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
                returned = json.loads(self.runner(script) or "[]")
                # Every uid the store returned, before dedup and before `_to_item` drops
                # all-day banners and cancellations. `seen_ids` answers "does the store
                # still have this?", which is not the same question as "did we emit it".
                # The connector suppresses one of two identical events across calendars
                # on purpose, and reading that suppression as a deletion would have
                # retracted live rows: the owner's HON 171, PSY 101 and CIS 236 all sit
                # in two calendars at once.
                for event in returned:
                    uid = str(event.get("uid") or "")
                    if uid:
                        self.seen_ids.add(uid)
                events.extend(returned)
                # Reached only when the query returned without raising, which is now the
                # honest signal it was not before: the JXA catch that used to wrap the
                # query turned a timeout into an empty list, so this line would have
                # certified a calendar nobody had actually read.
                self.complete_calendars.add(str(name))
            except Exception as exc:  # noqa: BLE001 — rule 5: one calendar is not the source
                # A calendar that times out or errors costs its own events and nothing
                # else. Losing one shared feed used to fail the whole connector and mark
                # the credential dead, which took the day plan's real meetings with it.
                #
                # Recorded ONLY in `failed_calendars`. It used to also write
                # `excluded_by_rule[f"calendar:{name}"]`, and that is how this hid: the
                # sync line rendered it as `boundary excluded 1 by rule
                # calendar:dkesava2@asu.edu`, which reads as a privacy rule doing its job
                # rather than as the owner's main calendar failing on every run. Rule 5
                # says a failing source is surfaced, and a failure wearing the vocabulary
                # of a deliberate exclusion is not surfaced.
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
        # Set last, and only here. `fetch` is a generator: a caller that abandons it
        # part-way never reaches this line, so a half-consumed read cannot certify a
        # window it did not finish returning.
        self.read_window = (starts_at.isoformat(), ends_before.isoformat())
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
