"""Is a source still working? Answered by counting rather than by a threshold.

Two rounds of auditing could not answer this honestly. A credential reading `ok` proves
only that the last run did not raise. The age of the newest row proves nothing at all:
on 2026-08-16 `apple-notes` had been silent for seventeen days and two separate checks
called its cursor parked — the store held 65 notes, the newest modified 2026-07-05, the
cursor sat at exactly that instant, and the owner had simply not written a note. A
days-since threshold would have been wrong about the only case available to calibrate it.

`upstream_count` asks the question the threshold was guessing at: does the store hold
items the ledger does not have? 65 against 65 is health, with nothing to tune. 65 against
40 is a cursor parked past its data, which is the real failure and is otherwise invisible.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from backglass.connectors.apple_notes import AppleNotesConnector
from backglass.connectors.base import Countable
from backglass.connectors.boundary import Boundary
from backglass.connectors.canvas_ics import CanvasIcsConnector
from backglass.ledger import USER_ID

NOTES: list[dict[str, Any]] = [
    {"id": "n-1", "name": "Fundraise", "body": "Follow up with Ravi",
     "modified": "2026-07-29T10:00:00.000Z", "created": "2026-07-01T09:00:00.000Z",
     "folder": "Work"},
    {"id": "n-2", "name": "Old", "body": "stale",
     "modified": "2026-07-01T08:00:00.000Z", "created": "2026-06-01T08:00:00.000Z",
     "folder": "Notes"},
    {"id": "n-3", "name": "Client", "body": "mail acme-client@example.com about it",
     "modified": "2026-07-15T08:00:00.000Z", "created": "2026-06-15T08:00:00.000Z",
     "folder": "Notes"},
]


def _runner(payload: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    def run(script: str) -> str:
        del script
        return json.dumps(payload)

    return run


class TestAppleNotesCounts:
    def test_it_counts_the_whole_store_not_what_is_new(self, boundary: Boundary) -> None:
        """The watermark is deliberately not applied. "What is new" is what `fetch`
        answers; this asks what the store holds in total, which is the only number the
        ledger's own count can be compared against."""
        connector = AppleNotesConnector(boundary=boundary, runner=_runner(NOTES))
        before = connector.upstream_count()
        list(connector.fetch(None))
        assert connector.cursor  # the watermark moved
        assert connector.upstream_count() == before

    def test_a_boundary_excluded_note_is_not_counted_as_missing(self) -> None:
        """The false alarm this would otherwise re-create in a new place: an excluded
        note is never stored, so counting it would report a permanent one-item gap on a
        source that is working exactly as designed."""
        excluding = Boundary(mode="deny", deny_domains=["example.com"])
        connector = AppleNotesConnector(boundary=excluding, runner=_runner(NOTES))
        stored = len(list(connector.fetch(None)))
        assert connector.excluded == 1, "the fixture must actually exclude something"
        assert connector.upstream_count() == stored == 2

    def test_a_store_that_cannot_be_read_is_unknown_not_zero(
        self, boundary: Boundary
    ) -> None:
        """None is a real answer. Reporting zero would say "the store is empty", which
        is the confident-answer-from-a-missing-input failure `state` exists to prevent."""
        def boom(_script: str) -> str:
            raise OSError("Notes automation unavailable")

        connector = AppleNotesConnector(boundary=boundary, runner=boom)
        assert connector.upstream_count() is None

    def test_it_satisfies_the_protocol(self, boundary: Boundary) -> None:
        connector = AppleNotesConnector(boundary=boundary, runner=_runner(NOTES))
        assert isinstance(connector, Countable)


class TestCanvasCounts:
    ICS = (
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VEVENT\r\nUID:event-assignment-1\r\nDTSTART;VALUE=DATE:20260904\r\n"
        "SUMMARY:Problem set [BIO 181]\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:event-assignment-2\r\nDTSTART;VALUE=DATE:20260905\r\n"
        "SUMMARY:Quiz [BIO 181]\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:event-calendar-event-9\r\nDTSTART;VALUE=DATE:20260904\r\n"
        "SUMMARY:Lecture [BIO 181]\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )

    def _connector(self, permissive: Boundary, body: str) -> CanvasIcsConnector:
        connector = CanvasIcsConnector(feed_url="https://x/y.ics", boundary=permissive)
        connector._get = lambda: body  # type: ignore[method-assign]
        return connector

    def test_a_class_meeting_is_not_a_missing_assignment(
        self, permissive: Boundary
    ) -> None:
        """The feed carries one VEVENT per assignment *and* per calendar event. Counting
        the lecture would report the ledger as permanently one behind."""
        connector = self._connector(permissive, self.ICS)
        assert connector.upstream_count() == 2
        assert len(list(connector.fetch(None))) == 2

    def test_a_revoked_feed_is_unknown_not_zero(self, permissive: Boundary) -> None:
        connector = CanvasIcsConnector(feed_url="https://x/y.ics", boundary=permissive)

        def boom() -> str:
            raise OSError("connection reset by peer")

        connector._get = boom  # type: ignore[method-assign]
        assert connector.upstream_count() is None


class TestDoctorReportsTheGap:
    """The check that turns the count into a sentence someone can act on."""

    @staticmethod
    def _rows(conn: sqlite3.Connection, source: str, n: int) -> None:
        for i in range(n):
            conn.execute(
                "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
                " occurred_at, title, body_text, content_hash, triage_verdict,"
                " extraction_version) VALUES (?, ?, ?, '2026-08-01T00:00:00Z',"
                " '2026-08-01T00:00:00Z', 'x', 'x', ?, 'keep', 'manual')",
                (USER_ID, source, f"{source}-{i}", f"h{source}{i}"),
            )

    @staticmethod
    def _probe(conn: sqlite3.Connection, connector: Any) -> list[tuple[str, bool, str]]:
        from backglass.__main__ import _check_reconciles

        seen: list[tuple[str, bool, str]] = []
        _check_reconciles(
            conn, connector, lambda label, ok, detail: seen.append((label, ok, detail))
        )
        return seen

    def test_a_matching_ledger_passes(
        self, conn: sqlite3.Connection, boundary: Boundary
    ) -> None:
        connector = AppleNotesConnector(boundary=boundary, runner=_runner(NOTES))
        self._rows(conn, "apple-notes", 3)
        [(label, ok, _)] = self._probe(conn, connector)
        assert label == "apple-notes fully ingested"
        assert ok

    def test_a_parked_cursor_is_named_with_its_remedy(
        self, conn: sqlite3.Connection, boundary: Boundary
    ) -> None:
        """The failure this exists for: the store holds three notes, the ledger holds
        one, and every other surface reads green."""
        connector = AppleNotesConnector(boundary=boundary, runner=_runner(NOTES))
        self._rows(conn, "apple-notes", 1)
        [(_, ok, detail)] = self._probe(conn, connector)
        assert not ok
        assert "store has 3, ledger has 1" in detail
        assert "2 never ingested" in detail
        assert "cursor = NULL" in detail

    def test_a_connector_that_cannot_count_says_nothing(
        self, conn: sqlite3.Connection, boundary: Boundary
    ) -> None:
        """Most sources are windowed or paged and will never implement this. A check
        that prints a line per source it cannot check is a check nobody finishes."""
        class Windowed:
            name = "apple-mail"

            def health(self) -> None: ...

        assert self._probe(conn, Windowed()) == []

    def test_an_unreadable_store_says_nothing_either(
        self, conn: sqlite3.Connection, boundary: Boundary
    ) -> None:
        """Unknown is not a failure. `health()` already reports an unreachable store, and
        raising it twice in different words trains the reader to skim both."""
        def boom(_script: str) -> str:
            raise OSError("Notes automation unavailable")

        connector = AppleNotesConnector(boundary=boundary, runner=boom)
        assert self._probe(conn, connector) == []


@pytest.fixture
def permissive() -> Boundary:
    return Boundary(mode="off")
