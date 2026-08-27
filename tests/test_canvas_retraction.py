"""An assignment Canvas no longer publishes, and the two ways that could go wrong.

Goal 4 increment B2, and it needed no new mechanism: `retraction.reconcile` already
retracts a connector's rows that its latest *certified complete* read did not return, and
`sync` already calls it for every connector. All Canvas was missing was the certification.

The two failures this guards are not hypothetical — `retraction.py`'s own docstring
records them coming one clean run from retracting sixteen live classes:

  * a read that failed looking like a store that emptied, and
  * `seen_ids` recording what the connector *emitted* rather than what the store returned,
    so anything filtered on the way out reads as deleted.
"""

from __future__ import annotations

import sqlite3

import pytest

from backglass import retraction
from backglass.config import Settings
from backglass.connectors.boundary import Boundary
from backglass.connectors.canvas_ics import CanvasIcsConnector
from backglass.db import now_iso
from backglass.ledger import USER_ID

FEED_HEAD = "BEGIN:VCALENDAR\nVERSION:2.0\n"
FEED_TAIL = "END:VCALENDAR\n"


def _event(assignment_id: str, title: str, due: str, course: str = "BIO 181") -> str:
    return (
        "BEGIN:VEVENT\n"
        f"UID:event-assignment-{assignment_id}@instructure.com\n"
        f"SUMMARY:{title} [{course}]\n"
        f"DTSTART;VALUE=DATE:{due.replace('-', '')}\n"
        f"URL:https://canvas.asu.edu/courses/1/assignments/{assignment_id}\n"
        "END:VEVENT\n"
    )


def _feed(*events: str) -> str:
    return FEED_HEAD + "".join(events) + FEED_TAIL


def _connector(monkeypatch: pytest.MonkeyPatch, body: str, settings: Settings):  # type: ignore[no-untyped-def]
    connector = CanvasIcsConnector(
        feed_url="https://canvas.example/feed.ics", boundary=Boundary.from_settings(settings)
    )
    monkeypatch.setattr(connector, "_get", lambda: body)
    return connector


def _stored(conn: sqlite3.Connection, assignment_id: str, due: str) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, content_hash, triage_verdict)"
        " VALUES (?, 'canvas:ics', ?, ?, ?, 'Canvas', 'a thing', 'b', ?, 'keep')",
        (USER_ID, f"assignment:{assignment_id}", now_iso(), due, f"h-{assignment_id}"),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


class TestCertification:
    def test_a_completed_read_certifies_what_it_saw(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        connector = _connector(
            monkeypatch,
            _feed(_event("1", "Problem set", "2026-09-01"), _event("2", "Quiz", "2026-09-08")),
            settings,
        )
        list(connector.fetch(None))

        window = connector.retractable_window()

        assert window is not None
        assert window.seen_ids == {"assignment:1", "assignment:2"}
        assert window.starts_at == "2026-09-01"
        assert window.ends_before.startswith("2026-09-08")
        assert window.calendars is None

    def test_a_read_that_never_ran_certifies_nothing(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        connector = _connector(
            monkeypatch, _feed(_event("1", "Problem set", "2026-09-01")), settings
        )

        assert connector.retractable_window() is None

    def test_a_read_that_raised_certifies_nothing(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        """"The feed returned nothing" and "the feed no longer has anything" are the same
        bytes to everything downstream, which is how a timeout becomes a deletion."""
        connector = _connector(monkeypatch, "", settings)

        def boom() -> str:
            raise TimeoutError("canvas took too long")

        monkeypatch.setattr(connector, "_get", boom)
        with pytest.raises(TimeoutError):
            list(connector.fetch(None))

        assert connector.retractable_window() is None

    def test_a_read_abandoned_partway_certifies_nothing(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        """The case `read_complete` actually exists for, and the only one the empty-set
        check cannot cover: the read *did* see assignments, and then stopped. A caller
        that takes the first item and walks away, or a document that fails to parse
        halfway, leaves a partial set that looks exactly like a shrunken feed.
        """
        connector = _connector(
            monkeypatch,
            _feed(
                _event("1", "Problem set", "2026-09-01"),
                _event("2", "Quiz", "2026-09-08"),
                _event("3", "Lab", "2026-09-15"),
            ),
            settings,
        )
        stream = connector.fetch(None)
        next(stream)  # one item, then abandoned — the generator never finishes

        assert connector.seen_ids, "precondition: it did see something"
        assert connector.retractable_window() is None

    def test_an_empty_document_certifies_nothing(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        """A feed that parses to zero assignments is far more likely to be a login page or
        a rotated URL than a semester that was deleted."""
        connector = _connector(monkeypatch, _feed(), settings)
        list(connector.fetch(None))

        assert connector.retractable_window() is None

    def test_an_excluded_assignment_still_counts_as_seen(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        """The documented near-miss. The boundary drops it on the way out, but Canvas
        published it — recording only what was emitted is what nearly retracted sixteen
        live classes."""
        # The boundary is a *deny* list here (config.boundary_mode is exclude/full_scope),
        # and the connector checks the bracketed course as the "address".
        sett = settings.model_copy(update={
            "boundary_mode": "exclude", "boundary_deny_domains": ["LAW 900"],
        })
        connector = _connector(
            monkeypatch,
            _feed(
                _event("1", "Problem set", "2026-09-01", course="BIO 181"),
                _event("2", "Client memo", "2026-09-02", course="LAW 900"),
            ),
            sett,
        )
        emitted = list(connector.fetch(None))

        window = connector.retractable_window()

        assert [item.external_id for item in emitted] == ["assignment:1"], "precondition"
        assert connector.excluded == 1
        assert window is not None
        assert "assignment:2" in window.seen_ids


class TestReconciling:
    def test_an_assignment_that_left_the_feed_is_retracted(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        gone = _stored(conn, "2", "2026-09-04")
        _stored(conn, "1", "2026-09-01")
        connector = _connector(
            monkeypatch,
            _feed(_event("1", "Problem set", "2026-09-01"), _event("3", "Lab", "2026-09-08")),
            settings,
        )
        list(connector.fetch(None))

        retracted = retraction.reconcile(conn, connector)

        assert retracted == [gone]

    def test_a_row_outside_the_window_is_left_alone(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        """Last semester's coursework is not deleted because this semester's feed does not
        mention it. Retracting on absence outside the read's span would erase history the
        moment a feed narrowed."""
        old = _stored(conn, "99", "2026-01-15")
        connector = _connector(
            monkeypatch,
            _feed(_event("1", "Problem set", "2026-09-01"), _event("3", "Lab", "2026-09-08")),
            settings,
        )
        list(connector.fetch(None))

        retracted = retraction.reconcile(conn, connector)

        assert old not in retracted

    def test_a_second_pass_retracts_nothing_new(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        """Rule 3. An unchanged feed on an unchanged ledger writes nothing the second
        time."""
        _stored(conn, "2", "2026-09-04")
        _stored(conn, "1", "2026-09-01")
        connector = _connector(
            monkeypatch,
            _feed(_event("1", "Problem set", "2026-09-01"), _event("3", "Lab", "2026-09-08")),
            settings,
        )
        list(connector.fetch(None))
        retraction.reconcile(conn, connector)

        list(connector.fetch(None))
        assert retraction.reconcile(conn, connector) == []
