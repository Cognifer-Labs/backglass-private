"""Retraction: what a complete re-read proves, and what a partial one must not.

The owner's class schedule changed on ~2026-08-10 and Backglass kept both timetables, so
the day planner subtracted five class slots that no longer meet. That is the bug these
tests pin. The more dangerous direction is the other one: `apple_calendar` fetches one
Apple Event per calendar and a timed-out calendar returns an empty list, which looks
exactly like a calendar somebody emptied. Every test below that involves a partial read
asserts that *nothing* happens, because a wrong retraction is invisible — the event simply
stops existing, and the owner finds out by missing it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from backglass import retraction
from backglass.db import now_iso
from backglass.ledger import USER_ID

WINDOW = ("2026-08-13T00:00:00+00:00", "2026-09-10T00:00:00+00:00")


@dataclass
class FakeConnector:
    """A windowed connector, standing in for `apple_calendar` at the protocol boundary."""

    window: tuple[str, str] | None = WINDOW
    seen: set[str] = field(default_factory=set)
    label: str = "calendar:apple"
    #: None = this read covers every sub-store the source has.
    calendars: set[str] | None = None

    @property
    def name(self) -> str:
        return self.label

    def retractable_window(self):  # type: ignore[no-untyped-def]
        if self.window is None:
            return None
        return retraction.RetractableWindow(
            starts_at=self.window[0],
            ends_before=self.window[1],
            seen_ids=set(self.seen),
            calendars=self.calendars,
        )


def an_event(
    conn: sqlite3.Connection,
    external_id: str,
    occurred_at: str,
    *,
    source: str = "calendar:apple",
    title: str = "BIO 181",
) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash, triage_verdict)"
        " VALUES (?, ?, ?, ?, ?, NULL, ?, '', '{}', ?, 'keep')",
        (USER_ID, source, external_id, now_iso(), occurred_at, title,
         f"h-{source}-{external_id}"),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


# ── 1. the bug it was written for ──────────────────────────────────────────


def test_an_event_the_store_no_longer_returns_is_retracted(conn) -> None:  # type: ignore[no-untyped-def]
    """LIA 101, dropped from the owner's schedule and still eating a Monday morning."""
    gone = an_event(conn, "lia-101", "2026-08-24T17:10:00+00:00", title="LIA 101")
    kept = an_event(conn, "chm-113", "2026-08-24T19:20:00+00:00", title="CHM 113")

    retracted = retraction.reconcile(conn, FakeConnector(seen={"chm-113"}))

    assert retracted == [gone]
    assert retraction.retracted_ids(conn) == {gone}
    assert kept not in retraction.retracted_ids(conn)


def test_the_source_item_itself_is_untouched(conn) -> None:  # type: ignore[no-untyped-def]
    """Migration 0002 makes source_item immutable and docs/03 keeps raw items forever.
    Next year's reader still gets to see that CHM 113 met on Wednesdays until the 10th."""
    item = an_event(conn, "lia-101", "2026-08-24T17:10:00+00:00")

    retraction.reconcile(conn, FakeConnector(seen={"other"}))

    row = conn.execute(
        "SELECT title, occurred_at FROM source_item WHERE id = ?", (item,)
    ).fetchone()
    assert row is not None and row["occurred_at"] == "2026-08-24T17:10:00+00:00"


# ── 2. the safety property ─────────────────────────────────────────────────


def test_a_connector_that_cannot_certify_its_read_retracts_nothing(conn) -> None:  # type: ignore[no-untyped-def]
    """`retractable_window` returning None is how a partial read says so. A calendar that
    timed out returns an empty event list, indistinguishable from an empty calendar."""
    an_event(conn, "lia-101", "2026-08-24T17:10:00+00:00")

    assert retraction.reconcile(conn, FakeConnector(window=None)) == []
    assert retraction.retracted_ids(conn) == set()


def test_a_connector_with_no_such_method_is_left_alone(conn) -> None:  # type: ignore[no-untyped-def]
    """Optional protocol, like `base.Countable`. An incremental connector must never
    implement it: a cursor means the read never asked for everything."""

    class Incremental:
        name = "apple-mail"

    an_event(conn, "m-1", "2026-08-24T17:10:00+00:00", source="apple-mail")

    assert retraction.reconcile(conn, Incremental()) == []


def test_rows_outside_the_window_are_never_retracted(conn) -> None:  # type: ignore[no-untyped-def]
    """The read said nothing about them. Retracting on absence outside the window would
    erase the owner's history the moment a connector narrowed its horizon."""
    before = an_event(conn, "old", "2026-07-01T17:10:00+00:00")
    after = an_event(conn, "far", "2026-12-01T17:10:00+00:00")

    assert retraction.reconcile(conn, FakeConnector(seen={"nothing"})) == []
    assert before not in retraction.retracted_ids(conn)
    assert after not in retraction.retracted_ids(conn)


def test_another_sources_rows_are_not_retracted(conn) -> None:  # type: ignore[no-untyped-def]
    """`calendar:asu` is a frozen manual import with no connector. A complete read of
    `calendar:apple` proves nothing whatever about it."""
    asu = an_event(conn, "asu-1", "2026-08-24T17:10:00+00:00", source="calendar:asu")

    retraction.reconcile(conn, FakeConnector(seen={"chm-113"}))

    assert asu not in retraction.retracted_ids(conn)


# ── 3. idempotency and reversal ────────────────────────────────────────────


def test_a_second_reconcile_writes_nothing(conn) -> None:  # type: ignore[no-untyped-def]
    """Rule 3. The second pass must find the row already retracted and return empty."""
    an_event(conn, "lia-101", "2026-08-24T17:10:00+00:00")
    connector = FakeConnector(seen={"chm-113"})

    first = retraction.reconcile(conn, connector)
    second = retraction.reconcile(conn, connector)

    assert len(first) == 1
    assert second == []
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM source_item_retraction"
    ).fetchone()["n"] == 1


def test_a_retraction_can_be_undone(conn) -> None:  # type: ignore[no-untyped-def]
    """A retraction is an inference. One the owner disagrees with must be answerable by
    something other than editing the database by hand."""
    item = an_event(conn, "lia-101", "2026-08-24T17:10:00+00:00")
    retraction.reconcile(conn, FakeConnector(seen={"chm-113"}))

    assert retraction.restore(conn, item) is True
    assert retraction.retracted_ids(conn) == set()
    assert retraction.restore(conn, item) is False


def test_an_event_that_comes_back_is_retracted_again_only_if_it_leaves_again(conn) -> None:  # type: ignore[no-untyped-def]
    """Restoring is the owner overruling the inference; a later complete read that still
    does not return the event is entitled to conclude the same thing again."""
    item = an_event(conn, "lia-101", "2026-08-24T17:10:00+00:00")
    retraction.reconcile(conn, FakeConnector(seen={"chm-113"}))
    retraction.restore(conn, item)

    assert retraction.reconcile(conn, FakeConnector(seen={"lia-101"})) == []
    assert retraction.reconcile(conn, FakeConnector(seen={"chm-113"})) == [item]


# ── 4. the planner stops counting it ───────────────────────────────────────


def test_capacity_stops_subtracting_a_retracted_event(conn) -> None:  # type: ignore[no-untyped-def]
    """The point of the whole path. Five dropped classes went on consuming the owner's
    day for ten days because nothing could say they were gone."""
    import json
    from datetime import date

    from backglass.plan import capacity

    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash, triage_verdict)"
        " VALUES (?, 'calendar:apple', 'lia-101', ?, '2026-08-24T10:10:00-07:00', NULL,"
        " 'LIA 101', '', ?, 'h-lia', 'keep')",
        (USER_ID, now_iso(), json.dumps({
            "starts_at": "2026-08-24T10:10:00-07:00",
            "ends_at": "2026-08-24T11:00:00-07:00",
            "status": "confirmed",
        })),
    )
    item = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    day = date(2026, 8, 24)

    before = capacity.fixed_events(conn, day, "America/Phoenix")
    conn.execute(
        "INSERT INTO source_item_retraction (source_item_id, user_id, retracted_at, reason)"
        " VALUES (?, ?, ?, 'test')",
        (item, USER_ID, now_iso()),
    )
    after = capacity.fixed_events(conn, day, "America/Phoenix")

    assert [e.title for e in before] == ["LIA 101"]
    assert after == []


# ── 5. per-calendar scope ──────────────────────────────────────────────────


def test_a_row_from_a_calendar_that_was_not_read_is_never_retracted(conn) -> None:  # type: ignore[no-untyped-def]
    """The scoping rule. The owner has one calendar that takes minutes and sometimes
    fails; its rows must be untouchable on a run that could not read it, while a healthy
    calendar's deletions still land. All-or-nothing let the slow one veto everything."""
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash, triage_verdict)"
        " VALUES (?, 'calendar:apple', 'unread-1', ?, '2026-08-24T17:10:00+00:00', NULL,"
        " 'LIA 101', '', ?, 'h-unread', 'keep')",
        (USER_ID, now_iso(), '{"calendar": "dkesava2@asu.edu"}'),
    )
    unread = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash, triage_verdict)"
        " VALUES (?, 'calendar:apple', 'read-1', ?, '2026-08-24T19:00:00+00:00', NULL,"
        " 'Old thing', '', ?, 'h-read', 'keep')",
        (USER_ID, now_iso(), '{"calendar": "Work"}'),
    )
    read = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    retracted = retraction.reconcile(
        conn, FakeConnector(seen={"something-else"}, calendars={"Work"})
    )

    assert retracted == [read]
    assert unread not in retraction.retracted_ids(conn)


def test_a_row_whose_calendar_cannot_be_determined_is_left_alone(conn) -> None:  # type: ignore[no-untyped-def]
    """A row we cannot place is not evidence of anything. The cost of a wrong retraction
    is an event vanishing from the owner's day; the cost of a missed one is a stale row
    the next clean read picks up anyway."""
    for external_id, raw in (("no-json", ""), ("bad-json", "{oops"), ("no-key", "{}")):
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, author, title, body_text, raw_json, content_hash,"
            " triage_verdict) VALUES (?, 'calendar:apple', ?, ?,"
            " '2026-08-24T17:10:00+00:00', NULL, 'X', '', ?, ?, 'keep')",
            (USER_ID, external_id, now_iso(), raw, f"h-{external_id}"),
        )

    retracted = retraction.reconcile(
        conn, FakeConnector(seen={"nothing"}, calendars={"Work"})
    )

    assert retracted == []
