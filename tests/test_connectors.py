"""Phase 5 — widen intake. docs/07 §Google Drive, §Notes, §Calendar, §Canvas.

docs/09 §Phase 5: "Each is a connector against an extraction pipeline that already works,
which is why this is late and cheap rather than early and expensive." So these tests are
mostly about the *rules each source has that the others do not* — Drive's ownership rule,
Canvas's quota rule, Calendar's declined-versus-tentative rule, Obsidian's absent-vault
rule — plus the one thing they all share: the docs/08 boundary runs before persistence in
every one of them.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from backglass.config import Settings
from backglass.connectors.boundary import Boundary
from backglass.connectors.calendar import CalendarConnector
from backglass.connectors.canvas import CanvasConnector
from backglass.connectors.drive import DriveConnector
from backglass.connectors.notes import NotesConnector
from backglass.ledger import Ledger
from backglass.plan import capacity as capacity_mod

DENY = ["clientexample.gov"]


@pytest.fixture
def enforcing() -> Boundary:
    return Boundary(mode="exclude", deny_domains=DENY)


# ── fakes ─────────────────────────────────────────────────────────────────


class _Req:
    def __init__(self, payload: Any):
        self._payload = payload

    def execute(self) -> Any:
        return self._payload


class FakeCalendarService:
    def __init__(self, events: list[dict[str, Any]], sync_token: str = "tok-1"):
        self.events_list = events
        self.sync_token = sync_token
        self.raises_on_sync = False

    def calendarList(self):  # noqa: N802
        service = self

        class _CL:
            def get(self, **kwargs: Any) -> _Req:
                del kwargs
                return _Req({"id": "primary"})

        del service
        return _CL()

    def events(self):  # noqa: D102
        service = self

        class _Events:
            def list(self, **kwargs: Any) -> _Req:
                if "syncToken" in kwargs and service.raises_on_sync:
                    raise RuntimeError("410 sync token expired")
                return _Req({"items": service.events_list, "nextSyncToken": service.sync_token})

            def list_next(self, request: Any, response: Any) -> None:
                del request, response
                return None

        return _Events()


class FakeDriveService:
    def __init__(self, files: list[dict[str, Any]], contents: dict[str, bytes] | None = None):
        self.files_list = files
        self.contents = contents or {}
        self.export_calls: list[str] = []

    def about(self):  # noqa: D102
        class _About:
            def get(self, **kwargs: Any) -> _Req:
                del kwargs
                return _Req({"user": {"emailAddress": "owner@example.com"}})

        return _About()

    def changes(self):  # noqa: D102
        class _Changes:
            def getStartPageToken(self) -> _Req:  # noqa: N802
                return _Req({"startPageToken": "page-1"})

        return _Changes()

    def files(self):  # noqa: D102
        service = self

        class _Files:
            def list(self, **kwargs: Any) -> _Req:
                service.last_list_kwargs = kwargs  # type: ignore[attr-defined]
                return _Req({"files": service.files_list})

            def list_next(self, request: Any, response: Any) -> None:
                del request, response
                return None

            def export(self, *, fileId: str, mimeType: str) -> _Req:  # noqa: N803
                del mimeType
                service.export_calls.append(fileId)
                return _Req(service.contents.get(fileId, b""))

            def get_media(self, *, fileId: str) -> _Req:  # noqa: N803
                return _Req(service.contents.get(fileId, b""))

        return _Files()


# ── Calendar ──────────────────────────────────────────────────────────────


def an_event(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": kwargs.pop("id", "e1"),
        "summary": kwargs.pop("summary", "Standup"),
        "status": kwargs.pop("status", "confirmed"),
        "start": {"dateTime": kwargs.pop("start", "2026-07-30T10:00:00-07:00")},
        "end": {"dateTime": kwargs.pop("end", "2026-07-30T11:00:00-07:00")},
        "organizer": {"email": kwargs.pop("organizer", "colleague@example.com")},
        "attendees": kwargs.pop("attendees", []),
    }
    base.update(kwargs)
    return base


def test_calendar_events_become_source_items(enforcing: Boundary) -> None:
    connector = CalendarConnector(
        label="personal", service=FakeCalendarService([an_event()]), boundary=enforcing
    )
    items = list(connector.fetch(None))
    assert len(items) == 1
    payload = json.loads(items[0].raw_json)
    assert payload["starts_at"] == "2026-07-30T10:00:00-07:00"
    assert connector.cursor == "tok-1"
    assert items[0].source == "calendar:personal"


def test_a_declined_event_is_marked_and_excluded_from_capacity(
    conn, settings: Settings, enforcing: Boundary
) -> None:  # type: ignore[no-untyped-def]
    """docs/07: "Declined events are excluded from capacity. Tentative events count as
    busy."."""
    declined = an_event(
        id="declined",
        attendees=[{"self": True, "responseStatus": "declined"}],
    )
    tentative = an_event(
        id="tentative",
        start="2026-07-30T13:00:00-07:00",
        end="2026-07-30T14:00:00-07:00",
        attendees=[{"self": True, "responseStatus": "tentative"}],
    )
    connector = CalendarConnector(
        label="personal",
        service=FakeCalendarService([declined, tentative]),
        boundary=enforcing,
    )
    ledger = Ledger(conn, settings)
    for item in connector.fetch(None):
        ledger.upsert_source_item(item)

    events = capacity_mod.fixed_events(conn, date(2026, 7, 30), "America/Phoenix")
    titles = {event.title for event in events}
    assert len(events) == 1, "the declined event must not consume capacity"
    assert titles == {"Standup"}


def test_a_travel_event_is_counted_separately(
    conn, settings: Settings, enforcing: Boundary
) -> None:  # type: ignore[no-untyped-def]
    """docs/04 §1.2 counts travel apart from meetings; a two-hour drive is not a meeting."""
    connector = CalendarConnector(
        label="personal",
        service=FakeCalendarService([an_event(id="t", summary="Drive to Coimbatore airport")]),
        boundary=enforcing,
    )
    ledger = Ledger(conn, settings)
    for item in connector.fetch(None):
        ledger.upsert_source_item(item)

    events = capacity_mod.fixed_events(conn, date(2026, 7, 30), "America/Phoenix")
    assert events[0].travel is True

    cap = capacity_mod.compute(conn, settings, date(2026, 7, 30), events=events)
    assert cap.travel_minutes == 60
    assert cap.fixed_minutes == 0


def test_an_all_day_event_is_not_capacity(enforcing: Boundary) -> None:
    event = an_event()
    event["start"] = {"date": "2026-07-30"}
    event["end"] = {"date": "2026-07-31"}
    connector = CalendarConnector(
        label="personal", service=FakeCalendarService([event]), boundary=enforcing
    )
    assert list(connector.fetch(None)) == []


def test_a_cancelled_event_is_skipped(enforcing: Boundary) -> None:
    connector = CalendarConnector(
        label="personal",
        service=FakeCalendarService([an_event(status="cancelled")]),
        boundary=enforcing,
    )
    assert list(connector.fetch(None)) == []


def test_an_expired_sync_token_degrades_to_a_window(enforcing: Boundary) -> None:
    """docs/07's cursor-loss rule, same as Gmail's historyId."""
    service = FakeCalendarService([an_event()])
    service.raises_on_sync = True
    connector = CalendarConnector(label="personal", service=service, boundary=enforcing)
    assert len(list(connector.fetch("stale-token"))) == 1


def test_the_boundary_runs_on_calendar_before_persistence(enforcing: Boundary) -> None:
    """docs/08 D1 applies to every connector, not just Gmail. An event with a client on
    the invite list is client correspondence."""
    event = an_event(
        id="client",
        attendees=[{"email": "dana@clientexample.gov"}],
    )
    connector = CalendarConnector(
        label="personal", service=FakeCalendarService([event]), boundary=enforcing
    )
    assert list(connector.fetch(None)) == []
    assert connector.excluded == 1
    assert connector.excluded_by_rule == {"clientexample.gov": 1}


# ── Drive ─────────────────────────────────────────────────────────────────


def a_file(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": kwargs.pop("id", "f1"),
        "name": kwargs.pop("name", "Migration plan"),
        "mimeType": kwargs.pop("mimeType", "application/vnd.google-apps.document"),
        "size": kwargs.pop("size", "1200"),
        "modifiedTime": kwargs.pop("modifiedTime", "2026-07-28T10:00:00Z"),
        "owners": kwargs.pop("owners", [{"emailAddress": "contactdharsan@gmail.com"}]),
        "permissions": kwargs.pop("permissions", []),
        "trashed": False,
    }
    base.update(kwargs)
    return base


def test_drive_extracts_text_from_a_doc(enforcing: Boundary) -> None:
    service = FakeDriveService([a_file()], {"f1": b"I'll send the revised plan by Friday."})
    connector = DriveConnector(
        label="personal",
        service=service,
        boundary=enforcing,
        owner_emails=("contactdharsan@gmail.com",),
    )
    items = list(connector.fetch(None))
    assert len(items) == 1
    assert "revised plan" in str(items[0].body_text)
    assert connector.cursor == "page-1"


def test_drive_never_crawls_shared_drives(enforcing: Boundary) -> None:
    """docs/07: "Only files owned by or explicitly shared with the owner. Do not crawl
    shared drives." A boundary requirement wearing a performance requirement's clothes."""
    service = FakeDriveService([a_file()], {"f1": b"text"})
    connector = DriveConnector(
        label="personal",
        service=service,
        boundary=enforcing,
        owner_emails=("contactdharsan@gmail.com",),
    )
    list(connector.fetch(None))
    kwargs = service.last_list_kwargs  # type: ignore[attr-defined]
    assert kwargs["corpora"] == "user"
    assert kwargs["includeItemsFromAllDrives"] is False
    assert kwargs["supportsAllDrives"] is False


def test_a_file_the_owner_cannot_reach_is_skipped(enforcing: Boundary) -> None:
    service = FakeDriveService(
        [a_file(owners=[{"emailAddress": "someone.else@example.com"}])], {"f1": b"text"}
    )
    connector = DriveConnector(
        label="personal",
        service=service,
        boundary=enforcing,
        owner_emails=("contactdharsan@gmail.com",),
    )
    assert list(connector.fetch(None)) == []


def test_binary_and_media_are_skipped(enforcing: Boundary) -> None:
    """docs/07: "Text extracted from Docs, Sheets, and PDFs. Binary and media skipped."."""
    service = FakeDriveService(
        [a_file(id="img", mimeType="image/png"), a_file(id="vid", mimeType="video/mp4")],
        {"img": b"\\x89PNG", "vid": b"\\x00"},
    )
    connector = DriveConnector(
        label="personal",
        service=service,
        boundary=enforcing,
        owner_emails=("contactdharsan@gmail.com",),
    )
    assert list(connector.fetch(None)) == []
    assert service.export_calls == [], "an image is never even fetched"


def test_a_drive_file_shared_with_a_denylisted_domain_is_excluded(enforcing: Boundary) -> None:
    """docs/08: "Attachments and Drive files inherit the same rule by ownership and
    sharing."."""
    service = FakeDriveService(
        [a_file(permissions=[{"emailAddress": "dana@clientexample.gov"}])], {"f1": b"text"}
    )
    connector = DriveConnector(
        label="personal",
        service=service,
        boundary=enforcing,
        owner_emails=("contactdharsan@gmail.com",),
    )
    assert list(connector.fetch(None)) == []
    assert connector.excluded == 1


# ── Notes (Obsidian) ──────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / ".obsidian").mkdir(parents=True)
    (root / ".obsidian" / "config.md").write_text("not a note")
    (root / "daily").mkdir()
    (root / "daily" / "2026-07-10.md").write_text(
        "---\ndate: 2026-07-10\ntitle: Thursday\n---\n\nSend Dana the scope by Friday.\n"
    )
    (root / "empty.md").write_text("---\ndate: 2026-07-11\n---\n\n   \n")
    return root


def test_notes_reads_a_vault(vault: Path, enforcing: Boundary) -> None:
    connector = NotesConnector(vault_path=vault, boundary=enforcing)
    items = list(connector.fetch(None))

    assert len(items) == 1, "empty notes and .obsidian config are skipped"
    item = items[0]
    assert item.source == "notes:obsidian"
    assert item.title == "Thursday"
    assert "Send Dana the scope" in str(item.body_text)
    assert "---" not in str(item.body_text), "frontmatter is stripped"


def test_a_notes_date_beats_its_mtime(vault: Path, enforcing: Boundary) -> None:
    """CLAUDE.md rule 4. A daily note dated 2026-07-10 and edited today must resolve
    "by Friday" against the 10th, not against today — otherwise editing an old note
    silently rewrites its deadlines."""
    connector = NotesConnector(vault_path=vault, boundary=enforcing)
    item = next(iter(connector.fetch(None)))
    assert item.occurred_at.startswith("2026-07-10")


def test_notes_is_incremental_by_mtime(vault: Path, enforcing: Boundary) -> None:
    connector = NotesConnector(vault_path=vault, boundary=enforcing)
    assert len(list(connector.fetch(None))) == 1
    cursor = connector.cursor
    assert cursor

    again = NotesConnector(vault_path=vault, boundary=enforcing)
    assert list(again.fetch(cursor)) == [], "unchanged notes are not re-read"

    (vault / "new.md").write_text("---\ndate: 2026-07-12\n---\n\nA new thought.\n")
    third = NotesConnector(vault_path=vault, boundary=enforcing)
    assert len(list(third.fetch(cursor))) == 1


def test_a_note_quoting_a_client_is_excluded(vault: Path, enforcing: Boundary) -> None:
    """docs/08 D1 does not care which connector is persisting. A note that pastes in a
    client thread is client correspondence."""
    (vault / "pasted.md").write_text(
        "---\ndate: 2026-07-12\n---\n\nFrom: dana@clientexample.gov\n\nthe WIC numbers\n"
    )
    connector = NotesConnector(vault_path=vault, boundary=enforcing)
    titles = {item.title for item in connector.fetch(None)}
    assert "pasted" not in titles
    assert connector.excluded == 1


def test_a_missing_vault_is_a_visible_failure(tmp_path: Path, enforcing: Boundary) -> None:
    """docs/10 §Deployment names this as the one connector that breaks if the pipeline
    moves off the machine holding the vault. Reporting zero notes would look like an empty
    vault, which is the failure docs/11 §8 calls dangerous."""
    connector = NotesConnector(vault_path=tmp_path / "nope", boundary=enforcing)
    health = connector.health()
    assert health.ok is False
    assert "machine holding it" in str(health.detail)
    with pytest.raises(FileNotFoundError):
        list(connector.fetch(None))


# ── Canvas ────────────────────────────────────────────────────────────────


class FakeCanvas(CanvasConnector):
    """Overrides only the HTTP layer, so pagination and the item mapping are real."""

    def __init__(self, pages: dict[str, tuple[Any, dict[str, str]]], **kwargs: Any):
        super().__init__(base_url="https://canvas.example.edu", token="t", **kwargs)
        self.pages = pages
        self.requested: list[str] = []

    def _get_url(self, url: str) -> tuple[Any, dict[str, str]]:
        self.requested.append(url)
        for key, value in self.pages.items():
            if key in url:
                return value
        return [], {}


def test_canvas_turns_an_unsubmitted_assignment_into_an_item(enforcing: Boundary) -> None:
    due = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    connector = FakeCanvas(
        {
            "/api/v1/courses?": (
                [{"id": 11, "name": "PUBHLTH 501"}],
                {},
            ),
            "/courses/11/assignments": (
                [
                    {
                        "id": 22,
                        "name": "Policy memo",
                        "due_at": due,
                        "updated_at": "2026-07-20T09:00:00Z",
                        "submission": {"workflow_state": "unsubmitted"},
                    }
                ],
                {},
            ),
        },
        boundary=enforcing,
    )
    items = list(connector.fetch(None))
    assert len(items) == 1
    assert "PUBHLTH 501" in str(items[0].title)
    assert "Policy memo" in str(items[0].body_text)
    assert json.loads(items[0].raw_json)["assignment_id"] == 22


def test_a_submitted_assignment_is_not_an_open_commitment(enforcing: Boundary) -> None:
    connector = FakeCanvas(
        {
            "/api/v1/courses?": ([{"id": 11, "name": "Course"}], {}),
            "/courses/11/assignments": (
                [
                    {
                        "id": 22,
                        "name": "Done already",
                        "due_at": "2026-07-25T23:59:00Z",
                        "submission": {"submitted_at": "2026-07-24T10:00:00Z"},
                    }
                ],
                {},
            ),
        },
        boundary=enforcing,
    )
    assert list(connector.fetch(None)) == []


def test_an_assignment_with_no_due_date_is_not_a_commitment(enforcing: Boundary) -> None:
    connector = FakeCanvas(
        {
            "/api/v1/courses?": ([{"id": 11, "name": "Course"}], {}),
            "/courses/11/assignments": ([{"id": 22, "name": "Someday", "due_at": None}], {}),
        },
        boundary=enforcing,
    )
    assert list(connector.fetch(None)) == []


def test_canvas_follows_rfc5988_link_pagination_to_completion(enforcing: Boundary) -> None:
    """docs/07: "Follow RFC 5988 Link header pagination to completion."."""
    # Ordered: the fake matches by substring and "/api/v1/courses?" also occurs inside
    # the page-2 URL, so the more specific key has to come first.
    connector = FakeCanvas(
        {
            "courses?page=2": ([{"id": 12, "name": "Two"}], {}),
            "/courses/11/assignments": ([], {}),
            "/courses/12/assignments": ([], {}),
            "/api/v1/courses?": (
                [{"id": 11, "name": "One"}],
                {"Link": '<https://canvas.example.edu/api/v1/courses?page=2>; rel="next"'},
            ),
        },
        boundary=enforcing,
    )
    list(connector.fetch(None))
    assert any("page=2" in url for url in connector.requested)
    assert any("/courses/12/assignments" in url for url in connector.requested)


def test_canvas_reports_a_rejected_token_as_the_documented_failure(enforcing: Boundary) -> None:
    """docs/07: "Some institutions disable student-generated tokens ... the fallback is
    the ICS feed, which loses submission state."."""

    class Rejecting(FakeCanvas):
        def _get_url(self, url: str) -> tuple[Any, dict[str, str]]:
            raise PermissionError("canvas rejected the token")

    health = Rejecting({}, boundary=enforcing).health()
    assert health.ok is False
    assert "Approved Integrations" in str(health.detail)
    assert "ICS feed" in str(health.detail)


def test_canvas_is_not_configured_is_a_health_state_not_a_crash(enforcing: Boundary) -> None:
    connector = CanvasConnector(base_url="", token="", boundary=enforcing)
    assert connector.health().ok is False


# ── all of them together ──────────────────────────────────────────────────


def test_every_connector_satisfies_the_protocol() -> None:
    """docs/07: "Every connector implements the same interface and writes only
    `source_item` rows."."""
    from backglass.connectors.base import Connector
    from backglass.connectors.gmail import GmailConnector

    boundary = Boundary(mode="full_scope")
    built = [
        GmailConnector(label="a", service=None, boundary=boundary),
        CalendarConnector(label="a", service=None, boundary=boundary),
        DriveConnector(label="a", service=None, boundary=boundary),
        NotesConnector(vault_path=Path("/tmp"), boundary=boundary),
        CanvasConnector(base_url="x", token="y", boundary=boundary),
    ]
    from backglass.connectors.github import GithubConnector
    from backglass.connectors.slack import SlackConnector

    built += [
        GithubConnector(token="t", boundary=boundary),
        SlackConnector(token="t", channel_ids=("C1",), boundary=boundary),
    ]
    for connector in built:
        assert isinstance(connector, Connector), type(connector)
        assert ":" in connector.name, "source names are prefixed so credential rows are unique"


def test_source_names_are_unique_per_account() -> None:
    """`credential` is UNIQUE(user_id, source), so two accounts of the same kind must not
    collide. This is the deviation recorded in tasks/todo.md #2, applied to every source."""
    boundary = Boundary(mode="full_scope")
    names = {
        CalendarConnector(label="personal", service=None, boundary=boundary).name,
        CalendarConnector(label="asu", service=None, boundary=boundary).name,
        DriveConnector(label="personal", service=None, boundary=boundary).name,
    }
    assert names == {"calendar:personal", "calendar:asu", "drive:personal"}


# ── Drive PDFs (docs/12 §2) ───────────────────────────────────────────────


def test_drive_extracts_pdf_text_instead_of_decoding_the_bytes(enforcing: Boundary) -> None:
    """Before pypdf this path UTF-8-decoded the download, which stored a PDF's object
    streams as if they were prose. It now goes through the same extractor the drop folder
    uses, so the two cannot drift."""
    from tests.test_files import make_pdf

    service = FakeDriveService(
        [a_file(mimeType="application/pdf", name="Signed scope.pdf")],
        {"f1": make_pdf("Countersigned scope due Friday")},
    )
    connector = DriveConnector(
        label="personal",
        service=service,
        boundary=enforcing,
        owner_emails=("contactdharsan@gmail.com",),
    )
    items = list(connector.fetch(None))

    assert len(items) == 1
    assert "Countersigned scope due Friday" in str(items[0].body_text)
    assert "%PDF" not in str(items[0].body_text), "object streams are not prose"


def test_a_drive_pdf_with_no_extractable_text_is_skipped(enforcing: Boundary) -> None:
    """The drop folder stores a scan and flags needs_ocr because the owner put it there on
    purpose. A Drive scan is one of thousands of files nobody pointed at, and _to_item
    already drops anything with no text — so it stays dropped rather than filling the
    ledger with empty items."""
    from tests.test_files import make_pdf

    service = FakeDriveService(
        [a_file(mimeType="application/pdf")], {"f1": make_pdf(None)}
    )
    connector = DriveConnector(
        label="personal",
        service=service,
        boundary=enforcing,
        owner_emails=("contactdharsan@gmail.com",),
    )
    assert list(connector.fetch(None)) == []


def test_an_unreadable_drive_pdf_does_not_break_the_run(enforcing: Boundary) -> None:
    """CLAUDE.md rule 5: a failing item degrades, never blocks."""
    service = FakeDriveService(
        [
            a_file(id="bad", mimeType="application/pdf"),
            a_file(id="good", name="Plan"),
        ],
        {"bad": b"%PDF-1.7 truncated", "good": b"The migration plan is due Monday."},
    )
    connector = DriveConnector(
        label="personal",
        service=service,
        boundary=enforcing,
        owner_emails=("contactdharsan@gmail.com",),
    )
    items = list(connector.fetch(None))

    assert [item.external_id for item in items] == ["good"]
