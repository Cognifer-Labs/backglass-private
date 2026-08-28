"""Canvas over the published ICS feed. docs/07 §Canvas, the fallback branch.

Built 2026-08-11 because ASU disables student-generated tokens — the failure docs/07
predicted, confirmed by Approved Integrations rendering its button inert. The feed is the
document the institution hands out; these tests hold the line that makes it worth having
(assignments become commitments, not calendar blocks) and the line it must not cross
(pretending to know what the API knows about submissions).

Nothing here touches the network: `_get` is replaced, so unfolding, escaping, the
assignment discriminator, the watermark and the field mapping are all real code paths.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import UTC, datetime
from typing import Any

import pytest

from backglass.connectors import canvas_ics as ics_module
from backglass.connectors.base import Connector
from backglass.connectors.boundary import Boundary
from backglass.connectors.canvas_ics import CanvasIcsConnector

FEED = "https://asu.instructure.com/feeds/calendars/user_abc123.ics"


@pytest.fixture
def permissive() -> Boundary:
    return Boundary(mode="full_scope")


@pytest.fixture
def enforcing() -> Boundary:
    return Boundary(mode="exclude", deny_domains=["clientexample.gov"])


def calendar(*events: str) -> str:
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(events) + "END:VCALENDAR\r\n"


def assignment_event(
    *,
    uid: str = "event-assignment-9001@asu.instructure.com",
    summary: str = "Problem set 3 [BIO 181]",
    dtstart: str = "DTSTART:20260828T065900Z",
    url: str = "https://asu.instructure.com/courses/101/assignments/9001",
) -> str:
    return (
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SUMMARY:{summary}\r\n"
        f"{dtstart}\r\n"
        f"URL:{url}\r\n"
        "END:VEVENT\r\n"
    )


def assignment_with_description(description: str) -> str:
    return (
        "BEGIN:VEVENT\r\n"
        "UID:event-assignment-7833000@asu.instructure.com\r\n"
        "SUMMARY:1-1-1 - Tech in the 21st Century (12:35) [CIS 236]\r\n"
        "DTSTART;VALUE=DATE:20260825\r\n"
        "URL:https://canvas.asu.edu/calendar#assignment_7833000\r\n"
        f"DESCRIPTION:{description}\r\n"
        "END:VEVENT\r\n"
    )


def class_meeting() -> str:
    return (
        "BEGIN:VEVENT\r\n"
        "UID:event-calendar-event-555@asu.instructure.com\r\n"
        "SUMMARY:BIO 181 Lecture [BIO 181]\r\n"
        "DTSTART:20260820T173000Z\r\n"
        "END:VEVENT\r\n"
    )


class FakeFeed(CanvasIcsConnector):
    """Overrides only the download."""

    def __init__(self, document: str, **kwargs: Any):
        kwargs.setdefault("feed_url", FEED)
        super().__init__(**kwargs)
        self.document = document
        self.downloads = 0

    def _get(self) -> str:
        self.downloads += 1
        return self.document


# ── 1. fixture-backed fetch ───────────────────────────────────────────────


class TestAnAssignmentBecomesAnObligation:
    def test_the_due_date_and_course_survive(self, permissive: Boundary) -> None:
        connector = FakeFeed(calendar(assignment_event()), boundary=permissive)

        items = list(connector.fetch(None))

        assert len(items) == 1
        item = items[0]
        assert item.source == "canvas:ics"
        assert item.external_id == "assignment:9001"
        assert item.title == "BIO 181 — Problem set 3"
        assert item.occurred_at == "2026-08-28T06:59:00+00:00"
        assert "is due 2026-08-28T06:59:00+00:00" in item.body_text

    def test_a_class_meeting_is_not_an_obligation(self, permissive: Boundary) -> None:
        """The feed carries both, distinguished only by the UID prefix. A lecture is what
        `apple_calendar` is for; ingesting it here would put every class on the board as
        something the owner owes somebody."""
        connector = FakeFeed(
            calendar(class_meeting(), assignment_event()), boundary=permissive
        )

        items = list(connector.fetch(None))

        assert [i.external_id for i in items] == ["assignment:9001"]

    def test_a_folded_title_is_reassembled_before_parsing(
        self, permissive: Boundary
    ) -> None:
        """RFC 5545 folds at 75 octets, which lands mid-word in exactly the long titles a
        student needs to recognise. Parsing without unfolding truncates them silently."""
        folded = (
            "BEGIN:VEVENT\r\n"
            "UID:event-assignment-9002@asu.instructure.com\r\n"
            "SUMMARY:Comparative analysis of dendritic cell antigen presentation in\r\n"
            "  disseminated coccidioidomycosis [BIO 181]\r\n"
            "DTSTART:20260901T065900Z\r\n"
            "END:VEVENT\r\n"
        )
        connector = FakeFeed(calendar(folded), boundary=permissive)

        item = next(iter(connector.fetch(None)))

        assert "disseminated coccidioidomycosis" in item.title
        assert "\n" not in item.title

    def test_escaped_text_is_unescaped(self, permissive: Boundary) -> None:
        connector = FakeFeed(
            calendar(assignment_event(summary="Read ch. 4\\, 5\\; then answer [BIO 181]")),
            boundary=permissive,
        )
        assert "ch. 4, 5; then answer" in next(iter(connector.fetch(None))).title

    def test_an_all_day_assignment_keeps_its_date_and_invents_no_hour(
        self, permissive: Boundary
    ) -> None:
        """docs/04: a plan with a day and no hour is not placed. Manufacturing a time here
        would launder a guess into the ledger as a fact."""
        connector = FakeFeed(
            calendar(assignment_event(dtstart="DTSTART;VALUE=DATE:20260828")),
            boundary=permissive,
        )

        item = next(iter(connector.fetch(None)))

        assert item.occurred_at == "2026-08-28"

    def test_an_event_with_no_date_is_skipped(self, permissive: Boundary) -> None:
        connector = FakeFeed(
            calendar(assignment_event(dtstart="DTSTART:not-a-date")), boundary=permissive
        )
        assert list(connector.fetch(None)) == []

    def test_a_truncated_download_drops_the_partial_event(
        self, permissive: Boundary
    ) -> None:
        """Half an assignment is not an assignment, and the next sync re-reads the whole
        document anyway."""
        cut = calendar(assignment_event()).replace("END:VCALENDAR\r\n", "")
        cut += "BEGIN:VEVENT\r\nUID:event-assignment-9999@asu\r\nSUMMARY:Half a row"
        connector = FakeFeed(cut, boundary=permissive)

        assert [i.external_id for i in connector.fetch(None)] == ["assignment:9001"]

    def test_the_cursor_records_the_read_not_a_due_date(self, permissive: Boundary) -> None:
        """A due-date watermark parked past the data and cost a semester; the cursor now
        says when the feed was read, which cannot get ahead of anything."""
        connector = FakeFeed(
            calendar(
                assignment_event(),
                assignment_event(
                    uid="event-assignment-9003@asu",
                    summary="Final [BIO 181]",
                    dtstart="DTSTART:20261210T065900Z",
                ),
            ),
            boundary=permissive,
        )

        list(connector.fetch(None))

        assert connector.cursor is not None
        stamp = datetime.fromisoformat(str(connector.cursor))
        assert stamp.tzinfo is not None
        assert abs((datetime.now(UTC) - stamp).total_seconds()) < 60

    def test_an_assignment_due_before_the_last_cursor_is_still_emitted(
        self, permissive: Boundary
    ) -> None:
        """The regression. A course publishes coursework due *earlier* than something
        already seen — a far-future "Excused Absence Requests" row is the real one — and
        under the old due-date watermark every such assignment was skipped forever."""
        connector = FakeFeed(
            calendar(
                assignment_event(),
                assignment_event(
                    uid="event-assignment-9004@asu",
                    summary="Excused Absence Requests [CHM 113]",
                    dtstart="DTSTART;VALUE=DATE:20270307",
                ),
            ),
            boundary=permissive,
        )

        emitted = [i.external_id for i in connector.fetch("2027-03-07")]

        assert emitted == ["assignment:9001", "assignment:9004"]

    def test_every_assignment_is_emitted_whatever_the_cursor_says(
        self, permissive: Boundary
    ) -> None:
        connector = FakeFeed(calendar(assignment_event()), boundary=permissive)
        assert len(list(connector.fetch("2099-01-01T00:00:00+00:00"))) == 1


# ── 2. idempotency ────────────────────────────────────────────────────────


def test_a_second_ingest_of_an_unchanged_feed_writes_nothing(
    conn, settings, permissive: Boundary  # type: ignore[no-untyped-def]
) -> None:
    """Idempotency at the ledger, not just at the hash.

    Matching hashes only prove the connector is deterministic. Now that `fetch` emits the
    whole feed on every read — the due-date watermark parked past the data and skipped a
    semester — `upsert_source_item`'s content_hash short-circuit is the *only* thing
    standing between 157 assignments a run and 157 writes a run. So it is asserted here,
    against the real table, rather than inferred.
    """
    from backglass.ledger import Ledger

    document = calendar(
        assignment_event(),
        assignment_event(
            uid="event-assignment-9004@asu",
            summary="Excused Absence Requests [CHM 113]",
            dtstart="DTSTART;VALUE=DATE:20270307",
        ),
    )

    first = Ledger(conn, settings)
    for item in FakeFeed(document, boundary=permissive).fetch(None):
        first.upsert_source_item(item)
    assert first.writes == 2

    second = Ledger(conn, settings)
    for item in FakeFeed(document, boundary=permissive).fetch(None):
        second.upsert_source_item(item)

    assert second.writes == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM source_item WHERE source = 'canvas:ics'"
    ).fetchone()["n"] == 2


def test_two_runs_over_an_unchanged_feed_hash_identically(permissive: Boundary) -> None:
    document = calendar(assignment_event())
    first = list(FakeFeed(document, boundary=permissive).fetch(None))
    second = list(FakeFeed(document, boundary=permissive).fetch(None))

    assert [i.content_hash for i in first] == [i.content_hash for i in second]


def test_a_moved_due_date_changes_the_hash(permissive: Boundary) -> None:
    """Idempotent must not mean blind. A moved deadline is the most important thing this
    source can report."""
    before = list(FakeFeed(calendar(assignment_event()), boundary=permissive).fetch(None))
    after = list(
        FakeFeed(
            calendar(assignment_event(dtstart="DTSTART:20260830T065900Z")),
            boundary=permissive,
        ).fetch(None)
    )

    assert before[0].external_id == after[0].external_id
    assert before[0].content_hash != after[0].content_hash


# ── 3. health, both ways ──────────────────────────────────────────────────


class TestHealthSaysWhichFailureThisIs:
    def test_an_unset_url_says_so_rather_than_calling_out(self, permissive: Boundary) -> None:
        health = CanvasIcsConnector(feed_url="", boundary=permissive).health()
        assert not health.ok
        assert "CANVAS_ICS_URL" in health.detail

    def test_a_real_calendar_is_healthy(self, permissive: Boundary) -> None:
        assert FakeFeed(calendar(assignment_event()), boundary=permissive).health().ok

    def test_an_html_page_is_not_mistaken_for_an_empty_semester(
        self, permissive: Boundary
    ) -> None:
        """The failure that would otherwise be silent forever: Canvas serves an HTML page
        with 200 for a reset feed, which parses to zero events and reads as "no
        assignments" rather than as a broken URL."""
        health = FakeFeed("<!DOCTYPE html><title>Canvas</title>", boundary=permissive).health()

        assert not health.ok
        assert "Calendar Feed" in health.detail

    def test_a_failing_download_never_leaks_the_feed_url(
        self, permissive: Boundary, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The URL is the credential here — it is per-user and unguessable, and anyone
        holding it reads the owner's coursework."""

        def raise_boom(*args: Any, **kwargs: Any) -> Any:
            raise urllib.error.URLError(f"failed to open {FEED}")

        monkeypatch.setattr(ics_module.urllib.request, "urlopen", raise_boom)
        connector = CanvasIcsConnector(
            feed_url=FEED, boundary=permissive, timeout_seconds=1
        )

        assert "user_abc123" not in connector.health().detail


def test_webcal_is_rewritten_rather_than_failing(
    permissive: Boundary, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Canvas offers the URL as webcal://. Calendar.app rewrites it silently and urllib
    does not, so a URL pasted straight out of Canvas would fail with "unknown url type"."""
    seen: list[str] = []

    class Response:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            return calendar(assignment_event()).encode()

    def capture(request: Any, timeout: float = 0) -> Any:
        seen.append(request.full_url)
        return Response()

    monkeypatch.setattr(ics_module.urllib.request, "urlopen", capture)
    connector = CanvasIcsConnector(
        feed_url=FEED.replace("https://", "webcal://"), boundary=permissive
    )

    assert connector.health().ok
    assert seen[0].startswith("https://")


# ── 4. boundary ───────────────────────────────────────────────────────────


def test_an_excluded_course_is_counted_and_never_stored(enforcing: Boundary) -> None:
    connector = FakeFeed(
        calendar(assignment_event(summary="Case notes [clientexample.gov]")),
        boundary=enforcing,
    )

    items = list(connector.fetch(None))

    assert items == []
    assert connector.excluded == 1
    assert sum(connector.excluded_by_rule.values()) == 1


def test_an_allowed_course_is_not_counted_as_excluded(enforcing: Boundary) -> None:
    connector = FakeFeed(calendar(assignment_event()), boundary=enforcing)
    assert len(list(connector.fetch(None))) == 1
    assert connector.excluded == 0


# ── what this connector must never claim ──────────────────────────────────


def test_the_row_says_it_does_not_know_about_submissions(permissive: Boundary) -> None:
    """The one thing separating this from the API connector, recorded on the item rather
    than only in a docstring: a reader asking why submitted work is still open should find
    the answer on the row."""
    item = next(iter(FakeFeed(calendar(assignment_event()), boundary=permissive).fetch(None)))

    assert json.loads(item.raw_json)["submission_state"].startswith("unknown")


def test_it_satisfies_the_connector_protocol(permissive: Boundary) -> None:
    connector = CanvasIcsConnector(feed_url=FEED, boundary=permissive)
    assert isinstance(connector, Connector)
    assert connector.name == "canvas:ics"


# ── goal 4: the half of the feed the item cannot carry ────────────────────────


def test_the_description_reaches_the_caller_even_though_the_item_cannot_hold_it(
    permissive: Boundary,
) -> None:
    """74 of the owner's 169 assignments carry a real DESCRIPTION — instructions, tool
    names, page counts — and the `SourceItem` carries one sentence by design. Widening
    the body is not an option: `ledger.upsert_source_item` treats a changed `content_hash`
    on a stored `external_id` as a conflict and skips it, so all 159 rows already in the
    ledger would produce errors and no new data. It rides on the connector instead."""
    feed = FakeFeed(
        calendar(assignment_with_description("Watch the video. Open Tableau.")),
        boundary=permissive,
    )

    items = list(feed.fetch(None))

    assert len(feed.assignments) == 1
    parsed = feed.assignments[0]
    assert parsed.description == "Watch the video. Open Tableau."
    assert parsed.title == "1-1-1 - Tech in the 21st Century (12:35)"
    assert parsed.course == "CIS 236"
    assert parsed.url.endswith("#assignment_7833000")
    # And the item is unchanged in the ways that matter: one terse sentence, and the
    # description only in `raw_json`, which `content_hash` does not cover.
    assert "Tableau" not in (items[0].body_text or "")
    assert json.loads(items[0].raw_json)["description"] == "Watch the video. Open Tableau."


def test_the_widened_raw_record_does_not_change_the_content_hash(
    permissive: Boundary,
) -> None:
    """The property that makes it free: 159 stored assignments keep the hash they were
    written with, so a re-read is still zero writes and zero errors."""
    with_text = FakeFeed(
        calendar(assignment_with_description("Open Tableau.")), boundary=permissive
    )
    without = FakeFeed(
        calendar(assignment_with_description("")), boundary=permissive
    )

    assert list(with_text.fetch(None))[0].content_hash == (
        list(without.fetch(None))[0].content_hash
    )


def test_an_excluded_course_contributes_no_assignment_either(
    enforcing: Boundary,
) -> None:
    """The boundary decides once, in `_parse`, so a course the owner excluded cannot
    reach the coursework record through the back door."""
    feed = FakeFeed(
        calendar(assignment_event(summary="Case notes [clientexample.gov]")),
        boundary=enforcing,
    )

    assert list(feed.fetch(None)) == []
    assert feed.assignments == []


def override_event(
    *,
    uid: str = "event-assignment-override-916149@asu.instructure.com",
    summary: str = "Individual Prelab Quiz 1: Glassware (Th9_Th10) [CHM 113 LABORATORY]",
    dtstart: str = "DTSTART;VALUE=DATE;VALUE=DATE:20260902",
    url: str = "https://canvas.asu.edu/calendar?include_contexts=course_262701#assignment_7494004",
) -> str:
    return (
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SUMMARY:{summary}\r\n"
        f"{dtstart}\r\n"
        f"URL:{url}\r\n"
        "END:VEVENT\r\n"
    )


class TestSectionOverrides:
    """The shape that silently cost a course.

    Canvas publishes an assignment whose due date belongs to the owner's section under
    `event-assignment-override-<id>`, *instead of* the base `event-assignment-<id>` and
    not beside it. `ASSIGNMENT_UID` wants a digit after `event-assignment-` and gets
    `override-`, so on 2026-08-27 twelve assignments — the whole of CHM 113 Laboratory's
    September coursework — were dropped by a connector reporting `ok`.
    """

    def test_a_section_dated_assignment_is_read(self, permissive: Boundary) -> None:
        feed = FakeFeed(calendar(override_event()), boundary=permissive)
        items = list(feed.fetch(None))
        assert [item.title for item in items] == [
            "CHM 113 LABORATORY — Individual Prelab Quiz 1: Glassware (Th9_Th10)"
        ]
        assert feed.assignments[0].course == "CHM 113 LABORATORY"
        assert feed.assignments[0].due_at == "2026-09-02"

    def test_it_is_keyed_by_the_assignment_not_the_override(
        self, permissive: Boundary
    ) -> None:
        """The override id names the override. Keying on it would make one assignment two
        rows the day its section date moved — and `source_item` is immutable, so the
        second row is permanent."""
        feed = FakeFeed(calendar(override_event()), boundary=permissive)
        items = list(feed.fetch(None))
        assert items[0].external_id == "assignment:7494004"
        assert feed.seen_ids == {"assignment:7494004"}

    def test_a_moved_section_date_rewrites_no_id(self, permissive: Boundary) -> None:
        moved = override_event(
            uid="event-assignment-override-999999@asu.instructure.com",
            dtstart="DTSTART;VALUE=DATE;VALUE=DATE:20260904",
        )
        feed = FakeFeed(calendar(moved), boundary=permissive)
        assert list(feed.fetch(None))[0].external_id == "assignment:7494004"

    def test_an_override_with_no_linkable_url_is_still_ingested(
        self, permissive: Boundary
    ) -> None:
        """An odd id is recoverable. A dropped assignment is not — which is the whole
        lesson of this class of bug."""
        feed = FakeFeed(calendar(override_event(url="")), boundary=permissive)
        assert list(feed.fetch(None))[0].external_id == "assignment:override-916149"

    def test_a_class_meeting_is_still_not_an_assignment(self, permissive: Boundary) -> None:
        feed = FakeFeed(calendar(class_meeting()), boundary=permissive)
        assert list(feed.fetch(None)) == []
