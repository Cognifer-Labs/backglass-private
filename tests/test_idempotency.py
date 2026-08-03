"""CLAUDE.md rule 3, and half of the Phase 1 exit criterion.

    "Idempotent by default. Two consecutive runs with no upstream changes produce zero
    writes. If you cannot assert this in a test, the sync is wrong."

    docs/09 §Phase 1: "...and a second run over unchanged input writes nothing."

The second run here is deliberately hostile: the Gmail history feed replays every message
it already delivered, which is what a real historyId window does when it overlaps. If
idempotency depended on the connector being well-behaved, this is where it would break.

The matrix at the bottom of the file says the same thing about every other source. Rule 3
is not a Gmail property — it is the property that lets launchd run this every 30 minutes
without the ledger drifting — and until that matrix existed the rule was asserted for the
one connector Phase 1 shipped and taken on faith for the thirteen added after it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from backglass.config import Settings
from backglass.connectors.anki import AnkiConnector
from backglass.connectors.apple_notes import AppleNotesConnector
from backglass.connectors.avorio import AvorioConnector
from backglass.connectors.base import Connector
from backglass.connectors.boundary import Boundary
from backglass.connectors.calendar import CalendarConnector
from backglass.connectors.drive import DriveConnector
from backglass.connectors.files import FilesConnector
from backglass.connectors.imessage import IMessageConnector
from backglass.connectors.instagram import (
    Allowlist,
    InstagramExportConnector,
    InstagramLiveConnector,
)
from backglass.connectors.notes import NotesConnector
from backglass.connectors.reminders import RemindersConnector
from backglass.connectors.slack import SlackConnector
from backglass.sync import sync
from tests.conftest import FakeGmailService, FakeModel, gmail_message, make_connector

SPECS: list[dict[str, Any]] = [
    {
        "id": "m1",
        "from": "Dana Whitfield <dwhitfield@example.gov>",
        "to": "alex.rivera@example.com",
        "subject": "Migration plan",
        "date": "Fri, 10 Jul 2026 09:15:00 -0700",
        "body": "I'll have the revised migration plan over to you by Friday.",
    },
    {
        "id": "m2",
        "from": "news@marketing.example.com",
        "to": "alex.rivera@example.com",
        "subject": "Weekly digest",
        "date": "Fri, 10 Jul 2026 06:00:00 -0700",
        "body": "Top stories this week.",
        "list_unsubscribe": "<mailto:unsub@marketing.example.com>",
    },
    {
        "id": "m3",
        "from": "no-reply@notifications.example.com",
        "to": "alex.rivera@example.com",
        "subject": "Your receipt",
        "date": "Fri, 10 Jul 2026 07:30:00 -0700",
        "body": "Thanks for your order.",
    },
]

MODEL_RESPONSES = {
    "Migration plan": {
        "commitments": [
            {
                "direction": "owed_to_me",
                "counterparty": "Dana Whitfield <dwhitfield@example.gov>",
                "what": "revised migration plan",
                "due_at": "2026-07-17",
                "due_is_explicit": True,
                "estimated_minutes": None,
                "confidence": 0.92,
                "evidence": "I'll have the revised migration plan over to you by Friday.",
                "resolves": False,
                "resolves_what": None,
            }
        ]
    },
}


@pytest.fixture
def messages() -> list[dict[str, Any]]:
    return [gmail_message(spec) for spec in SPECS]


def fresh_connector(messages: list[dict[str, Any]], boundary: Boundary):  # type: ignore[no-untyped-def]
    """A new connector each run, as the CLI builds one per invocation."""
    return make_connector(messages, boundary)


def test_second_run_over_unchanged_input_writes_nothing(
    conn, settings: Settings, boundary: Boundary, messages: list[dict[str, Any]]
) -> None:  # type: ignore[no-untyped-def]
    model = FakeModel(MODEL_RESPONSES)

    first = sync(conn, settings, [fresh_connector(messages, boundary)], model)
    assert first.writes > 0, "the first run must actually do something"
    assert first.fetched == 3
    assert first.commitments_inserted == 1
    calls_after_first = len(model.calls)

    second = sync(conn, settings, [fresh_connector(messages, boundary)], model)

    assert second.writes == 0, (
        f"second run wrote {second.writes} times; "
        f"stats={second.commitments_inserted} commitments, {second.extracted} extractions"
    )
    assert second.commitments_inserted == 0
    assert second.extracted == 0
    assert len(model.calls) == calls_after_first, (
        "the second run must not call the model at all"
    )


def test_the_second_run_replays_the_same_messages_and_still_writes_nothing(
    conn, settings: Settings, boundary: Boundary, messages: list[dict[str, Any]]
) -> None:  # type: ignore[no-untyped-def]
    """The history feed genuinely re-delivers on the second pass — that is the point.

    content_hash is what absorbs it. Without the hash short-circuit this test fails with
    three duplicate source_item conflicts, not with a unique-constraint error, which is
    why the assertion is on `writes` rather than on row counts.
    """
    model = FakeModel(MODEL_RESPONSES)
    sync(conn, settings, [fresh_connector(messages, boundary)], model)

    connector = fresh_connector(messages, boundary)
    second = sync(conn, settings, [connector], model)

    assert second.fetched == 3, "the fake history feed should re-deliver all three"
    assert second.writes == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM source_item").fetchone()["n"] == 3


def test_run_telemetry_is_not_counted_as_a_write(
    conn, settings: Settings, boundary, messages
) -> None:  # type: ignore[no-untyped-def]
    """tasks/todo.md §Deviations #7: `writes` counts domain writes, not the run row."""
    model = FakeModel(MODEL_RESPONSES)
    sync(conn, settings, [fresh_connector(messages, boundary)], model)
    sync(conn, settings, [fresh_connector(messages, boundary)], model)

    runs = list(conn.execute("SELECT writes FROM run ORDER BY id"))
    assert len(runs) == 2, "both runs are recorded"
    assert runs[1]["writes"] == 0


def test_dry_run_writes_nothing_at_all(
    conn, settings: Settings, boundary: Boundary, messages: list[dict[str, Any]]
) -> None:  # type: ignore[no-untyped-def]
    """docs/10 §CLI: --dry-run "prints the diff and writes nothing"."""
    model = FakeModel(MODEL_RESPONSES)
    report = sync(conn, settings, [fresh_connector(messages, boundary)], model, dry_run=True)

    assert report.writes > 0, "dry run still reports what it would have written"
    for table in ("source_item", "commitment", "entity", "run", "credential"):
        count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        assert count == 0, f"dry run wrote {count} row(s) to {table}"


def test_rules_drop_before_the_model_is_asked(
    conn, settings: Settings, boundary: Boundary, messages: list[dict[str, Any]]
) -> None:
    """Tier 0 is what keeps the bill small: two of three messages never reach a model."""
    model = FakeModel(MODEL_RESPONSES)
    report = sync(conn, settings, [fresh_connector(messages, boundary)], model)

    assert report.rule_dropped == 2, "the newsletter and the no-reply receipt are rule kills"
    assert report.model_triaged == 1
    triage_calls = [tier for tier, _ in model.calls if tier == "triage"]
    assert len(triage_calls) == 1


def test_a_failing_source_degrades_and_does_not_block(
    conn, settings: Settings, boundary: Boundary, messages: list[dict[str, Any]]
) -> None:
    """CLAUDE.md rule 5, and docs/07 §Health: auth expiry is a visible product state.

    The healthy mailbox must still be ingested, the failure must be recorded against the
    credential row so the Sources panel can show it, and the process must exit non-zero.
    """
    from backglass.connectors.gmail import GmailConnector

    broken_service = FakeGmailService(messages)
    broken_service.profile_raises = True
    broken_service.history_raises = True
    broken = GmailConnector(label="work", service=broken_service, boundary=boundary)
    # _full_scan calls getProfile, so a raising profile fails the whole fetch.
    healthy = make_connector(messages, boundary, label="personal")

    model = FakeModel(MODEL_RESPONSES)
    report = sync(conn, settings, [broken, healthy], model)

    assert "gmail:work" in report.failed_sources
    assert report.exit_code == 1
    assert report.fetched == 3, "the healthy mailbox still ingested"

    row = conn.execute(
        "SELECT status, last_error FROM credential WHERE source = 'gmail:work'"
    ).fetchone()
    assert row["status"] == "failed"
    assert row["last_error"]

    health = broken.health()
    assert health.ok is False
    assert health.detail is not None


def test_spend_cap_degrades_to_triage_only(
    conn, settings: Settings, boundary: Boundary, messages: list[dict[str, Any]]
) -> None:
    """CLAUDE.md rule 7 and docs/02 §Cost control.

    "When hit, degrade to triage-only rather than silently overspending." Extraction is
    skipped, the run is marked degraded, and the reason is surfaced rather than logged.
    """
    capped = settings.model_copy(update={"monthly_spend_cap_cents": 0})
    model = FakeModel(MODEL_RESPONSES)
    report = sync(conn, capped, [fresh_connector(messages, boundary)], model)

    assert report.degraded is True
    assert report.extracted == 0
    assert report.commitments_inserted == 0
    assert any("spend cap reached" in error for error in report.errors)
    assert conn.execute("SELECT degraded FROM run").fetchone()["degraded"] == 1


def test_oversized_body_is_parked_not_extracted(
    conn, settings: Settings, boundary: Boundary
) -> None:
    """docs/02 §Cost control: per_item_ceiling — "skip and park anything whose input
    exceeds it"."""
    huge = {
        "id": "huge",
        "from": "Dana <dwhitfield@example.gov>",
        "to": "alex.rivera@example.com",
        "subject": "Migration plan",
        "date": "Fri, 10 Jul 2026 09:15:00 -0700",
        "body": "I'll send the plan by Friday. " + ("x" * 70_000),
    }
    model = FakeModel(MODEL_RESPONSES)
    report = sync(conn, settings, [make_connector([gmail_message(huge)], boundary)], model)

    assert report.parked == 1
    assert report.extracted == 0
    assert any("per-item ceiling" in error for error in report.errors)
    assert report.exit_code == 1


# ── the same assertion, once per source ───────────────────────────────────
#
# One table rather than thirteen near-identical modules, because the assertion is
# identical for all of them and only the fixture differs: rule 3 is a property of the
# sync, and a per-connector copy of this test would drift into thirteen slightly
# different definitions of "wrote nothing".
#
# Each entry hands back a *factory*, not a connector. The CLI builds a fresh connector
# per invocation and re-loads the cursor from the credential row (sync.py `_ingest`), so
# a test that reused one instance across both runs would be asserting against in-process
# state the real scheduler never has — and would go green on a connector whose cursor is
# never persisted at all.
#
# Every fixture below is a fake, a temp file or a temp SQLite store built in the test.
# docs/10 §Testing: "No test calls a live third-party API" — and none of these may touch
# the owner's real Notes, Messages, Anki or drop folder either.

TZ = "America/Phoenix"

#: A zero-argument builder standing in for one CLI invocation.
Factory = Callable[[], Connector]


def _calendar(tmp_path: Path, boundary: Boundary) -> Factory:
    from tests.test_connectors import FakeCalendarService, an_event

    events = [an_event()]
    return lambda: CalendarConnector(
        label="personal", service=FakeCalendarService(events), boundary=boundary
    )


def _drive(tmp_path: Path, boundary: Boundary) -> Factory:
    from tests.test_connectors import FakeDriveService, a_file

    files = [a_file()]
    contents = {"f1": b"I'll send the revised plan by Friday."}
    return lambda: DriveConnector(
        label="personal",
        service=FakeDriveService(files, contents),
        boundary=boundary,
        owner_emails=("alex.rivera@example.com",),
    )


def _canvas(tmp_path: Path, boundary: Boundary) -> Factory:
    from tests.test_connectors import FakeCanvas

    due = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    pages = {
        "/api/v1/courses?": ([{"id": 11, "name": "PUBHLTH 501"}], {}),
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
    }
    return lambda: FakeCanvas(pages, boundary=boundary)


def _notes(tmp_path: Path, boundary: Boundary) -> Factory:
    vault = tmp_path / "vault"
    (vault / "daily").mkdir(parents=True)
    (vault / "daily" / "2026-07-10.md").write_text(
        "---\ndate: 2026-07-10\ntitle: Thursday\n---\n\nSend Dana the scope by Friday.\n"
    )
    return lambda: NotesConnector(vault_path=vault, boundary=boundary)


def _apple_notes(tmp_path: Path, boundary: Boundary) -> Factory:
    from tests.test_apple_sources import NOTES, fake_runner

    return lambda: AppleNotesConnector(boundary=boundary, runner=fake_runner(NOTES))


def _reminders(tmp_path: Path, boundary: Boundary) -> Factory:
    from tests.test_apple_sources import REMINDERS, fake_runner

    return lambda: RemindersConnector(boundary=boundary, runner=fake_runner(REMINDERS))


def _files(tmp_path: Path, boundary: Boundary) -> Factory:
    folder = tmp_path / "drop"
    folder.mkdir()
    (folder / "letter.txt").write_text("Send Dana the signed scope by Friday.")
    return lambda: FilesConnector(folder_path=folder, boundary=boundary)


def _github(tmp_path: Path, boundary: Boundary) -> Factory:
    from tests.test_github import FakeGithub, an_issue, one_page

    pages = one_page([an_issue()])
    return lambda: FakeGithub(pages, boundary=boundary)


def _slack(tmp_path: Path, boundary: Boundary) -> Factory:
    from tests.test_slack import CHANNEL, FakeSlack, a_message, a_page

    pages = {CHANNEL: [a_page([a_message()])]}
    return lambda: SlackConnector(
        token="xoxb-test",
        channel_ids=(CHANNEL,),
        boundary=boundary,
        transport=FakeSlack(pages),
    )


def _anki(tmp_path: Path, boundary: Boundary) -> Factory:
    from tests.test_med_phase_a import _anki_db

    path = _anki_db(tmp_path)
    return lambda: AnkiConnector(db_path=path, tz=TZ)


def _avorio(tmp_path: Path, boundary: Boundary) -> Factory:
    from tests.test_med_phase_a import _avorio_db

    path = _avorio_db(tmp_path)
    return lambda: AvorioConnector(db_path=path, tz=TZ)


def _imessage(tmp_path: Path, boundary: Boundary) -> Factory:
    from backglass.connectors.allowlist import Allowlist
    from tests.test_imessage import build_store

    path = build_store(
        tmp_path / "chat.db",
        [
            {
                "rowid": 1,
                "handle": "+14805551212",
                "text": "Can you send the deck by Friday?",
                "chat": "Phoenix build",
            }
        ],
    )
    return lambda: IMessageConnector(
        db_path=path, boundary=boundary, allowlist=Allowlist(("Phoenix build", "a@example.com"))
    )


def _instagram_export(tmp_path: Path, boundary: Boundary) -> Factory:
    from tests.test_instagram import TS_2026_07_10, build_export

    root = build_export(
        tmp_path / "ig",
        [
            {
                "key": "goatrip_123",
                "title": "Goa trip",
                "participants": ["K", "Priya", "Arjun"],
                "messages": [("Priya", TS_2026_07_10, "beach house saturday?")],
            }
        ],
    )
    return lambda: InstagramExportConnector(
        export_path=root, allowlist=Allowlist(["Goa trip"]), boundary=boundary
    )


def _instagram_live(tmp_path: Path, boundary: Boundary) -> Factory:
    from tests.test_instagram import a_thread, fake_client

    threads = [
        a_thread(
            "t1",
            "Goa trip",
            [(1, "priya.s")],
            [("m1", 1, "beach house saturday?", datetime(2026, 7, 10, 15, 4, 5, tzinfo=UTC))],
        )
    ]
    return lambda: InstagramLiveConnector(
        username="k",
        session_file=None,
        allowlist=Allowlist(["Goa trip"]),
        boundary=boundary,
        client_factory=lambda: fake_client(threads),
    )


#: (source name, fixture builder). The name is the pytest id, so a failure names the
#: connector that broke rather than a parameter index.
SOURCES: list[tuple[str, Callable[[Path, Boundary], Factory]]] = [
    ("calendar", _calendar),
    ("drive", _drive),
    ("canvas", _canvas),
    ("notes", _notes),
    ("apple-notes", _apple_notes),
    ("reminders", _reminders),
    ("files", _files),
    ("github", _github),
    ("slack", _slack),
    ("anki", _anki),
    ("avorio", _avorio),
    ("imessage", _imessage),
    ("instagram", _instagram_export),
    ("instagram:live", _instagram_live),
]


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Every domain table's row count, `run` excluded.

    `run` is telemetry: a second run legitimately inserts one (tasks/todo.md §Deviations
    #7), and counting it would make the assertion unsatisfiable. Read off sqlite_master
    rather than a hard-coded list so a table added by a later migration is covered the
    day it lands, not the day someone remembers this file.
    """
    tables = [
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name <> 'run' ORDER BY name"
        )
    ]
    return {
        table: int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
        for table in tables
    }


@pytest.mark.parametrize(("name", "build"), SOURCES, ids=[name for name, _ in SOURCES])
def test_a_second_run_over_an_unchanged_source_writes_nothing(
    conn: sqlite3.Connection,
    settings: Settings,
    boundary: Boundary,
    tmp_path: Path,
    name: str,
    build: Callable[[Path, Boundary], Factory],
) -> None:
    factory = build(tmp_path, boundary)
    model = FakeModel()

    first = sync(conn, settings, [factory()], model)
    # Without these three the test would pass just as happily on a connector that fetches
    # nothing, fails outright, or is never reached — the shape of "weak green" this matrix
    # exists to rule out.
    assert not first.failed_sources, first.errors
    assert first.fetched > 0, f"{name} ingested nothing; the fixture, not the sync, is wrong"
    assert first.writes > 0, "the first run must actually do something"

    before = _row_counts(conn)
    calls_after_first = len(model.calls)

    second = sync(conn, settings, [factory()], model)

    assert second.writes == 0, (
        f"{name} wrote {second.writes} times on an unchanged second run; "
        f"stats={second.commitments_inserted} commitments, {second.extracted} extractions"
    )
    assert _row_counts(conn) == before, f"{name} changed the row counts on the second run"
    assert len(model.calls) == calls_after_first, (
        f"{name} re-triaged or re-extracted an item it had already read — the bill this "
        f"costs is per-run, not once"
    )


@pytest.mark.parametrize(("name", "build"), SOURCES, ids=[name for name, _ in SOURCES])
def test_a_rescan_after_cursor_loss_still_writes_nothing(
    conn: sqlite3.Connection,
    settings: Settings,
    boundary: Boundary,
    tmp_path: Path,
    name: str,
    build: Callable[[Path, Boundary], Factory],
) -> None:
    """The hostile half, and the one the Gmail test above gets for free.

    Most of these connectors carry a watermark, so their ordinary second run fetches
    nothing and proves only that the cursor round-trips. Cursor loss is a documented
    state for every one of them — an expired Calendar sync token, a Gmail historyId past
    its window, an unreadable Anki cursor — and docs/07's answer is always the same: fall
    back to a window and let content_hash absorb the overlap. Clearing the credential
    cursor between the runs is that state, and it is the only version of this test that
    exercises the hash short-circuit rather than the watermark arithmetic.
    """
    factory = build(tmp_path, boundary)
    model = FakeModel()

    first = sync(conn, settings, [factory()], model)
    assert not first.failed_sources, first.errors
    assert first.fetched > 0

    before = _row_counts(conn)
    conn.execute("UPDATE credential SET cursor = NULL")

    second = sync(conn, settings, [factory()], model)

    assert second.fetched > 0, f"{name} did not actually rescan; the test proves nothing"
    assert second.writes == 0, f"{name} wrote {second.writes} times re-reading its own items"
    assert _row_counts(conn) == before
