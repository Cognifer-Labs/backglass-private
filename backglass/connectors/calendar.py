"""Google Calendar. docs/07 §Calendar.

  - Read-only. "The planner proposes; it never writes."
  - Declined events are excluded from capacity. Tentative events count as busy.
  - Events tagged travel get their own handling in the capacity model (docs/04 §1.2).

Calendar items are the one source that exists to feed the *capacity model* rather than the
extractor. They are written as ordinary `source_item` rows with the event-specific fields
in `raw_json`, which is what docs/03 prescribes — "if a field only makes sense for one
source, it belongs in raw_json, not in a column" — and is why `plan/capacity.py` needed no
schema change to consume them.

They are also dropped by the tier-0 rule layer (docs/02: "Calendar invite (handled by the
calendar connector, not extraction)"), so they cost nothing at the model layer.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from backglass.connectors.base import Cursor, Health, SourceItem, content_hash
from backglass.connectors.boundary import Boundary

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

#: docs/07: "Declined events are excluded from capacity. Tentative events count as busy."
DECLINED = "declined"

#: How a travel event is recognised. docs/04 §1.2 counts travel separately from meetings
#: because a two-hour drive is not a two-hour meeting and the buffer rules differ.
TRAVEL_HINTS = ("travel", "flight", "drive", "commute", "transit", "airport")


def _is_travel(summary: str, event: dict[str, Any]) -> bool:
    text = f"{summary} {event.get('description', '')}".lower()
    if any(hint in text for hint in TRAVEL_HINTS):
        return True
    return str(event.get("eventType", "")).lower() in (
        "outOfOffice".lower(),
        "focusTime".lower(),
    )


def _self_status(event: dict[str, Any]) -> str:
    for attendee in event.get("attendees", []) or []:
        if attendee.get("self"):
            return str(attendee.get("responseStatus", "accepted"))
    return "accepted"


@dataclass
class CalendarConnector:
    """One calendar. `label` distinguishes accounts, as with Gmail."""

    label: str
    service: Any
    boundary: Boundary
    calendar_id: str = "primary"
    #: How far ahead to look. The planner only ever plans one day, but the weekly capacity
    #: check in docs/04 §2.3 needs the coming week, and Monday planning needs it on Monday.
    horizon_days: int = 21
    #: The past matters for the estimate/actual report and for a brief regenerated for an
    #: earlier date.
    lookback_days: int = 7

    cursor: Cursor = None
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)
    _error: str | None = None

    @property
    def name(self) -> str:
        return f"calendar:{self.label}"

    def health(self) -> Health:
        if self._error:
            return Health(name=self.name, ok=False, detail=self._error)
        try:
            self.service.calendarList().get(calendarId=self.calendar_id).execute()
        except Exception as exc:  # noqa: BLE001
            return Health(name=self.name, ok=False, detail=_safe(exc))
        return Health(name=self.name, ok=True)

    def fetch(self, since: Cursor) -> Iterator[SourceItem]:
        """Incremental via `syncToken`, falling back to a windowed list.

        Google invalidates a syncToken with a 410 when it is too old; docs/07's cursor-loss
        rule applies exactly as it does to Gmail's historyId, so that degrades to a full
        window rather than failing the source.
        """
        self.excluded = 0
        self.excluded_by_rule = {}
        now = datetime.now(UTC)

        request: Any
        if since:
            try:
                request = self.service.events().list(
                    calendarId=self.calendar_id, syncToken=since, singleEvents=True
                )
                yield from self._drain(request)
                return
            except Exception:  # noqa: BLE001 - cursor loss degrades to a full window
                pass

        request = self.service.events().list(
            calendarId=self.calendar_id,
            singleEvents=True,
            orderBy="startTime",
            timeMin=(now - timedelta(days=self.lookback_days)).isoformat(),
            timeMax=(now + timedelta(days=self.horizon_days)).isoformat(),
        )
        yield from self._drain(request)

    def _drain(self, request: Any) -> Iterator[SourceItem]:
        while request is not None:
            response = request.execute()
            if response.get("nextSyncToken"):
                self.cursor = str(response["nextSyncToken"])
            for event in response.get("items", []) or []:
                item = self._to_item(event)
                if item is not None:
                    yield item
            request = self.service.events().list_next(request, response)

    def _to_item(self, event: dict[str, Any]) -> SourceItem | None:
        if event.get("status") == "cancelled":
            return None
        start = (event.get("start") or {}).get("dateTime")
        end = (event.get("end") or {}).get("dateTime")
        if not start or not end:
            # All-day events have `date` rather than `dateTime`. They do not consume a
            # working window the way a meeting does, so they are not capacity.
            return None

        summary = str(event.get("summary") or "Busy")
        organiser = (event.get("organizer") or {}).get("email", "")
        attendees = [a.get("email", "") for a in event.get("attendees", []) or []]

        # The boundary runs here, before persistence, exactly as in the Gmail connector.
        # An event with a client on the invite list is client correspondence.
        verdict = self.boundary.check([organiser, *attendees])
        if not verdict.allowed:
            self.excluded += 1
            rule = verdict.matched_rule or "?"
            self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + 1
            return None

        declined = _self_status(event) == DECLINED
        payload = {
            "starts_at": start,
            "ends_at": end,
            "status": event.get("status", "confirmed"),
            "declined": declined,
            "travel": _is_travel(summary, event),
            "organizer": organiser,
            "attendee_count": len(attendees),
            "html_link": event.get("htmlLink"),
        }
        return SourceItem(
            source=self.name,
            external_id=str(event["id"]),
            occurred_at=start,
            author=organiser or None,
            title=summary,
            # No body: docs/07 says calendar is handled by the connector, not extraction,
            # and a description full of dial-in details is not evidence of a commitment.
            body_text=None,
            raw_json=json.dumps(payload, sort_keys=True),
            content_hash=content_hash(
                author=organiser,
                title=summary,
                body_text=json.dumps(payload, sort_keys=True),
                occurred_at=start,
            ),
        )


def _safe(exc: Exception) -> str:
    import re

    text = f"{type(exc).__name__}: {exc}"
    return re.sub(r"(access_token|key|token)=[^&\s]+", r"\1=[redacted]", text)[:500]
