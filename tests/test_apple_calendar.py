"""Calendar.app through the automation bridge.

The runner is injected, so nothing here touches osascript or the machine's real
calendars — docs/10 §Testing: no test depends on what happens to be installed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from backglass.config import Settings
from backglass.connectors.apple_calendar import AppleCalendarConnector
from backglass.connectors.boundary import Boundary

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def event(**overrides: Any) -> dict[str, Any]:
    base = {
        "calendar": "Work",
        "uid": "UID-1",
        "title": "BIO 181",
        "starts_at": "2026-08-20T22:00:00.000Z",
        "ends_at": "2026-08-20T23:15:00.000Z",
        "all_day": False,
        "location": "Tempe MUR 101",
        "status": "",
    }
    base.update(overrides)
    return base


def fake_runner(events: list[dict[str, Any]], fail: set[str] | None = None):  # type: ignore[no-untyped-def]
    """Answers the two scripts differently, because the connector asks two questions.

    A stub that returned the same payload to both would make every test pass for the
    wrong reason: the calendar-listing call would come back as a list of event objects,
    each would be queried, and the cross-calendar dedup would quietly collapse the
    duplicates back down to the expected answer.
    """
    fail = fail or set()

    def run(script: str) -> str:
        if "const wanted = " not in script and "calendars().map" not in script:
            return "Calendar"  # the health probe
        if "calendars().map" in script:
            names = sorted({str(e.get("calendar") or "") for e in events})
            return json.dumps(names)
        wanted = json.loads(script.split("const wanted = ", 1)[1].split(";", 1)[0])
        if wanted in fail:
            raise RuntimeError("AppleEvent timed out. (-1712)")
        return json.dumps([e for e in events if e.get("calendar") == wanted])

    return run


def connector(settings: Settings, events: list[dict[str, Any]], **kw: Any):  # type: ignore[no-untyped-def]
    fail = kw.pop("fail", None)
    return AppleCalendarConnector(
        boundary=Boundary.from_settings(settings),
        runner=fake_runner(events, fail),
        now=lambda: NOW,
        **kw,
    )


class TestFetch:
    def test_an_event_becomes_a_calendar_source_item(self, settings: Settings) -> None:
        """The source name is `calendar:apple` because `plan/capacity.py::fixed_events`
        selects `source LIKE 'calendar%'` — matching that contract is what makes these
        events subtract from the day's capacity with no change to the planner."""
        (item,) = list(connector(settings, [event()]).fetch(None))

        assert item.source == "calendar:apple"
        assert item.external_id == "UID-1"
        assert item.occurred_at == "2026-08-20T22:00:00.000Z"
        payload = json.loads(item.raw_json or "{}")
        assert payload["starts_at"] == "2026-08-20T22:00:00.000Z"
        assert payload["ends_at"] == "2026-08-20T23:15:00.000Z"
        assert payload["declined"] is False
        assert payload["status"] == "confirmed"

    def test_the_same_class_in_two_calendars_is_one_event(self, settings: Settings) -> None:
        """Found on the owner's real machine, not imagined: three of nine events were the
        same class sitting in both a local "Work" calendar and a local "Family" one under
        different UIDs. Undeduplicated, the planner subtracts each lecture from the day
        twice and silently halves a teaching day.
        """
        rows = list(
            connector(
                settings,
                [event(calendar="Work", uid="A"), event(calendar="Family", uid="B")],
            ).fetch(None)
        )
        assert len(rows) == 1

    def test_the_surviving_twin_is_the_same_one_on_every_run(self, settings: Settings) -> None:
        """Chosen by sorting, not by arrival order — otherwise the ledger churns between
        two spellings of one event every time the bridge returns them in a new order."""
        both = [event(calendar="Work", uid="A"), event(calendar="Family", uid="B")]
        first = list(connector(settings, both).fetch(None))
        second = list(connector(settings, list(reversed(both))).fetch(None))
        assert [i.external_id for i in first] == [i.external_id for i in second]

    def test_two_genuinely_different_events_both_survive(self, settings: Settings) -> None:
        rows = list(
            connector(
                settings,
                [
                    event(uid="A"),
                    event(uid="B", title="PSY 101", starts_at="2026-08-20T19:00:00Z"),
                ],
            ).fetch(None)
        )
        assert len(rows) == 2

    def test_an_all_day_event_is_not_capacity(self, settings: Settings) -> None:
        """docs/07: an all-day event does not consume a working window the way a meeting
        does. Same call the Google connector makes."""
        assert list(connector(settings, [event(all_day=True)]).fetch(None)) == []

    def test_a_cancelled_event_is_dropped(self, settings: Settings) -> None:
        assert list(connector(settings, [event(status="cancelled")]).fetch(None)) == []

    def test_a_skipped_calendar_contributes_nothing(self, settings: Settings) -> None:
        """Subscribed holiday and birthday feeds would otherwise eat the day planner's
        capacity every week."""
        rows = list(
            connector(
                settings,
                [event(calendar="US Holidays")],
                skip=("us holidays",),
            ).fetch(None)
        )
        assert rows == []

    def test_a_skipped_calendar_does_not_suppress_its_twin(self, settings: Settings) -> None:
        """The skip has to be applied before the duplicate is chosen, or a holiday-feed
        copy of a real event claims the slot and takes the real one down with it."""
        rows = list(
            connector(
                settings,
                [event(calendar="US Holidays", uid="A"), event(calendar="Work", uid="B")],
                skip=("us holidays",),
            ).fetch(None)
        )
        assert [i.external_id for i in rows] == ["B"]

    def test_travel_is_flagged_separately(self, settings: Settings) -> None:
        (item,) = list(connector(settings, [event(title="Flight to Phoenix")]).fetch(None))
        assert json.loads(item.raw_json or "{}")["travel"] is True

    def test_an_unchanged_event_hashes_identically(self, settings: Settings) -> None:
        """The connector is windowed rather than incremental, so rule 3 rests entirely on
        the content hash recognising an unchanged event."""
        first = list(connector(settings, [event()]).fetch(None))
        second = list(connector(settings, [event()]).fetch(None))
        assert first[0].content_hash == second[0].content_hash

    def test_an_edited_event_hashes_differently(self, settings: Settings) -> None:
        first = list(connector(settings, [event()]).fetch(None))
        moved = list(connector(settings, [event(starts_at="2026-08-20T23:00:00Z")]).fetch(None))
        assert first[0].content_hash != moved[0].content_hash


class TestHealth:
    def test_a_denied_bridge_is_a_product_state_not_a_crash(self, settings: Settings) -> None:
        def refuse(_script: str) -> str:
            raise RuntimeError("Not authorised to send Apple events to Calendar.")

        health = AppleCalendarConnector(
            boundary=Boundary.from_settings(settings), runner=refuse
        ).health()
        assert health.ok is False
        assert "Automation" in (health.detail or "")

    def test_a_working_bridge_is_healthy(self, settings: Settings) -> None:
        health = connector(settings, []).health()
        assert health.ok is True


class TestOneCalendarIsNotTheSource:
    def test_a_calendar_that_times_out_costs_only_its_own_events(
        self, settings: Settings
    ) -> None:
        """The failure this split exists for. Asking for every calendar at once is one
        long Apple Event, macOS caps those at about two minutes regardless of the
        subprocess timeout, and the owner's machine returned `-1712` and marked the whole
        source dead — taking the day plan's real meetings with it.
        """
        rows = list(
            connector(
                settings,
                [
                    event(calendar="Work", uid="A"),
                    event(calendar="Shared", uid="B", title="PSY 101"),
                ],
                fail={"Shared"},
            ).fetch(None)
        )

        assert [i.external_id for i in rows] == ["A"], "the healthy calendar still lands"

    def test_the_failure_is_reported_rather_than_swallowed(self, settings: Settings) -> None:
        """A silently short day plan is worse than a loud one: nothing else on the page
        says a calendar is missing."""
        connector_ = connector(
            settings, [event(calendar="Shared", uid="B")], fail={"Shared"}
        )
        list(connector_.fetch(None))

        assert connector_.failed_calendars
        assert "Shared" in connector_.failed_calendars[0]
        assert "-1712" in connector_.failed_calendars[0]

    def test_a_skipped_calendar_is_never_queried(self, settings: Settings) -> None:
        """Each query is an Apple Event, and not spending one on a subscribed holiday feed
        is most of what keeps a run inside the ceiling. A skipped calendar that still got
        queried would also raise from `fail` here."""
        rows = list(
            connector(
                settings,
                [event(calendar="US Holidays", uid="H"), event(calendar="Work", uid="A")],
                skip=("us holidays",),
                fail={"US Holidays"},
            ).fetch(None)
        )

        assert [i.external_id for i in rows] == ["A"]
