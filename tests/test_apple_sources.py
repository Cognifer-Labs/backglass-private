"""Apple Notes + Reminders via the JXA bridge. Phase 7.

The runner is injected, so no test touches osascript — same rule as every other
connector: fakes, never the live surface.
"""

from __future__ import annotations

import json
from typing import Any

from backglass.connectors.apple_notes import AppleNotesConnector
from backglass.connectors.base import Connector
from backglass.connectors.boundary import Boundary
from backglass.connectors.reminders import RemindersConnector


def fake_runner(payload: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    def run(script: str) -> str:
        del script
        return json.dumps(payload)

    return run


NOTES = [
    {"id": "n-2", "name": "Fundraise notes", "body": "Follow up with Ravi by Friday",
     "modified": "2026-07-29T10:00:00.000Z", "created": "2026-07-01T09:00:00.000Z",
     "folder": "Work"},
    {"id": "n-1", "name": "Old note", "body": "stale",
     "modified": "2026-07-01T08:00:00.000Z", "created": "2026-06-01T08:00:00.000Z",
     "folder": "Notes"},
]

REMINDERS = [
    {"id": "r-1", "name": "Send Prof. Alvarez the draft", "body": "",
     "list": "School", "completed": False, "completionDate": None,
     "dueDate": "2026-08-04T17:00:00.000Z", "creationDate": "2026-07-28T09:00:00.000Z"},
    {"id": "r-2", "name": "Renew passport", "body": "bring photos",
     "list": "Personal", "completed": True,
     "completionDate": "2026-07-29T15:00:00.000Z", "dueDate": None,
     "creationDate": "2026-07-10T09:00:00.000Z"},
]


class TestAppleNotes:
    def test_satisfies_protocol_and_maps_fields(self, boundary: Boundary) -> None:
        connector = AppleNotesConnector(boundary=boundary, runner=fake_runner(NOTES))
        assert isinstance(connector, Connector)
        items = list(connector.fetch(None))
        assert [i.external_id for i in items] == ["n-1", "n-2"]  # oldest-first
        newest = items[1]
        assert newest.source == "apple-notes"
        assert newest.occurred_at == "2026-07-29T10:00:00.000Z"
        assert "Ravi" in (newest.body_text or "")
        assert connector.cursor == "2026-07-29T10:00:00.000Z"

    def test_watermark_fetches_nothing_the_second_time(self, boundary: Boundary) -> None:
        connector = AppleNotesConnector(boundary=boundary, runner=fake_runner(NOTES))
        list(connector.fetch(None))
        again = AppleNotesConnector(boundary=boundary, runner=fake_runner(NOTES))
        assert list(again.fetch(connector.cursor)) == []
        assert again.cursor == connector.cursor  # held, not reset

    def test_health_names_automation_permission_on_failure(
        self, boundary: Boundary
    ) -> None:
        def broken(script: str) -> str:
            raise RuntimeError("Error: Application isn't running (-600)")

        connector = AppleNotesConnector(boundary=boundary, runner=broken)
        health = connector.health()
        assert not health.ok
        assert "Automation" in (health.detail or "")


class TestReminders:
    def test_open_and_completed_map_to_distinct_items(self, boundary: Boundary) -> None:
        connector = RemindersConnector(boundary=boundary, runner=fake_runner(REMINDERS))
        items = {i.external_id: i for i in connector.fetch(None)}
        assert set(items) == {"r-1", "r-2:completed:2026-07-29T15:00:00.000Z"}
        open_item = items["r-1"]
        # Relative dates resolve against the reminder's own creation time.
        assert open_item.occurred_at == "2026-07-28T09:00:00.000Z"
        assert "due 2026-08-04" in (open_item.body_text or "")
        done_item = items["r-2:completed:2026-07-29T15:00:00.000Z"]
        assert done_item.occurred_at == "2026-07-29T15:00:00.000Z"
        assert "completed" in (done_item.body_text or "")

    def test_watermark_is_the_newest_timestamp_seen_not_the_wall_clock(
        self, boundary: Boundary
    ) -> None:
        """A completion landing between the snapshot and the cursor write is lost forever.

        The JXA filter drops anything completed at or before the stored cursor, so a
        cursor taken from the wall clock covers a window this run never read: the
        completion is absent from the snapshot and skipped by every run after it, and
        nothing anywhere reports the commitment as resolved.
        """
        connector = RemindersConnector(boundary=boundary, runner=fake_runner(REMINDERS))
        list(connector.fetch(None))
        assert connector.cursor == "2026-07-29T15:00:00.000Z"

    def test_a_run_that_sees_nothing_holds_the_watermark(self, boundary: Boundary) -> None:
        connector = RemindersConnector(boundary=boundary, runner=fake_runner([]))
        assert list(connector.fetch("2026-07-29T15:00:00.000Z")) == []
        assert connector.cursor == "2026-07-29T15:00:00.000Z"

    def test_recompletion_is_its_own_event_not_an_immutability_conflict(
        self, boundary: Boundary
    ) -> None:
        """Complete → un-complete → re-complete. Two completions, two ids.

        Sharing one external_id makes the second completion a differing content_hash for
        a stored row, which upsert_source_item records as a conflict and drops.
        """
        first = RemindersConnector(boundary=boundary, runner=fake_runner(REMINDERS))
        again = [
            dict(REMINDERS[1], completionDate="2026-07-31T11:00:00.000Z"),
        ]
        second = RemindersConnector(boundary=boundary, runner=fake_runner(again))

        ids = {i.external_id for i in first.fetch(None)} | {
            i.external_id for i in second.fetch(first.cursor)
        }
        assert "r-2:completed:2026-07-29T15:00:00.000Z" in ids
        assert "r-2:completed:2026-07-31T11:00:00.000Z" in ids

    def test_rereading_unchanged_reminders_is_write_free_by_hash(
        self, boundary: Boundary
    ) -> None:
        first = RemindersConnector(boundary=boundary, runner=fake_runner(REMINDERS))
        second = RemindersConnector(boundary=boundary, runner=fake_runner(REMINDERS))
        one = sorted(i.content_hash for i in first.fetch(None))
        two = sorted(i.content_hash for i in second.fetch(first.cursor))
        # No modification cursor exists in the scripting bridge; identical hashes are
        # what make the window-watermark re-read cost zero writes.
        assert one == two

    def test_boundary_excludes_and_counts(self, boundary: Boundary) -> None:
        tainted = [dict(REMINDERS[0], body="cc client@denied.example")]
        import backglass.connectors.boundary as boundary_mod

        strict = boundary_mod.Boundary(
            mode="exclude", deny_domains=("denied.example",), deny_addresses=()
        )
        connector = RemindersConnector(boundary=strict, runner=fake_runner(tainted))
        assert list(connector.fetch(None)) == []
        assert connector.excluded == 1
