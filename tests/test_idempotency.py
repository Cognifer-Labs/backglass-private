"""CLAUDE.md rule 3, and half of the Phase 1 exit criterion.

    "Idempotent by default. Two consecutive runs with no upstream changes produce zero
    writes. If you cannot assert this in a test, the sync is wrong."

    docs/09 §Phase 1: "...and a second run over unchanged input writes nothing."

The second run here is deliberately hostile: the Gmail history feed replays every message
it already delivered, which is what a real historyId window does when it overlaps. If
idempotency depended on the connector being well-behaved, this is where it would break.
"""

from __future__ import annotations

from typing import Any

import pytest

from backglass.config import Settings
from backglass.connectors.boundary import Boundary
from backglass.sync import sync
from tests.conftest import FakeGmailService, FakeModel, gmail_message, make_connector

SPECS: list[dict[str, Any]] = [
    {
        "id": "m1",
        "from": "Dana Whitfield <dwhitfield@example.gov>",
        "to": "contactdharsan@gmail.com",
        "subject": "Migration plan",
        "date": "Fri, 10 Jul 2026 09:15:00 -0700",
        "body": "I'll have the revised migration plan over to you by Friday.",
    },
    {
        "id": "m2",
        "from": "news@marketing.example.com",
        "to": "contactdharsan@gmail.com",
        "subject": "Weekly digest",
        "date": "Fri, 10 Jul 2026 06:00:00 -0700",
        "body": "Top stories this week.",
        "list_unsubscribe": "<mailto:unsub@marketing.example.com>",
    },
    {
        "id": "m3",
        "from": "no-reply@notifications.example.com",
        "to": "contactdharsan@gmail.com",
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
    broken = GmailConnector(label="asu", service=broken_service, boundary=boundary)
    # _full_scan calls getProfile, so a raising profile fails the whole fetch.
    healthy = make_connector(messages, boundary, label="personal")

    model = FakeModel(MODEL_RESPONSES)
    report = sync(conn, settings, [broken, healthy], model)

    assert "gmail:asu" in report.failed_sources
    assert report.exit_code == 1
    assert report.fetched == 3, "the healthy mailbox still ingested"

    row = conn.execute(
        "SELECT status, last_error FROM credential WHERE source = 'gmail:asu'"
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
        "to": "contactdharsan@gmail.com",
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
