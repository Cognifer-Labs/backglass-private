"""The fixture set required at the bottom of extract-commitments.md.

    "Every change to this prompt requires the fixture set in tests/fixtures/commitments/
    to pass. The fixtures must include, at minimum: ..." — nine cases, all present.

These are pipeline tests, not evals. The model response is canned, so what is under test
is §Post-processing steps 1 through 5 — entity resolution, estimates, the confidence
threshold, supersession and dedup — which are deterministic and belong in CI. Whether the
model produces those responses is measured separately, in evals/, and never gates CI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backglass.config import Settings
from backglass.extract.commitments import apply
from backglass.extract.engagements import apply as apply_engagements
from backglass.extract.schemas import CommitmentExtraction
from backglass.ledger import Ledger
from tests.conftest import FIXTURES, gmail_message, make_connector

CASES = sorted((FIXTURES / "commitments").glob("*.json"))
assert len(CASES) == 9, f"extract-commitments.md requires nine cases, found {len(CASES)}"


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def ingest(case: dict[str, Any], ledger: Ledger, boundary: Any) -> tuple[int, str]:
    """Put the fixture message through the real connector so occurred_at is real."""
    connector = make_connector([gmail_message(case["message"])], boundary)
    item = next(iter(connector.fetch(None)))
    item_id, _ = ledger.upsert_source_item(item)
    return item_id, item.occurred_at


def run_case(case: dict[str, Any], ledger: Ledger, settings: Settings, boundary: Any):  # type: ignore[no-untyped-def]
    """One response, both record types — the pairing `sync.py` applies in one transaction.

    The fixtures used to apply only the commitment half, which made a fixture structurally
    incapable of asking the question v8 of the prompt turns on: whether a stated hour
    became an engagement the planner can place, or a commitment with the time stranded in
    `what`. A case that cannot express the wrong answer cannot catch it.
    """
    item_id, occurred_at = ingest(case, ledger, boundary)
    extraction = CommitmentExtraction.model_validate(case["model_response"])
    report = apply(
        extraction,
        source_item_id=item_id,
        occurred_at=occurred_at,
        ledger=ledger,
        settings=settings,
    )
    plans = apply_engagements(
        extraction,
        source_item_id=item_id,
        occurred_at=occurred_at,
        ledger=ledger,
        settings=settings,
    )
    return report, plans


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_fixture(path: Path, conn, settings: Settings, boundary) -> None:  # type: ignore[no-untyped-def]
    case = load(path)
    ledger = Ledger(conn, settings)

    # 05 resolves a commitment created by 01, so its precondition runs first.
    if case.get("requires"):
        run_case(load(FIXTURES / "commitments" / case["requires"]), ledger, settings, boundary)

    report, plans = run_case(case, ledger, settings, boundary)
    expect = case["expect"]

    assert report.inserted == expect["inserted"], case["name"]
    assert report.deduped == expect["deduped"], case["name"]
    assert report.superseded == expect["superseded"], case["name"]
    assert report.review_queue == expect["review_queue"], case["name"]

    # v8 of the prompt turns on this pairing: an hour the owner must BE somewhere is an
    # engagement the planner can place, and an hour something is DUE by is a commitment.
    # Absent keys mean "no engagements", which is what the first seven cases expect.
    assert plans.inserted == expect.get("engagements_inserted", 0), case["name"]
    starts = [
        str(row["starts_at"])
        for row in conn.execute(
            "SELECT starts_at FROM engagement WHERE source_item_id = "
            "(SELECT id FROM source_item WHERE external_id = ?) ORDER BY id",
            (case["message"]["id"],),
        )
    ]
    assert starts == expect.get("engagement_starts_at", []), case["name"]

    rows = list(
        conn.execute(
            "SELECT due_at, estimate_source, confidence FROM commitment "
            "WHERE status = 'open' AND source_item_id = "
            "(SELECT id FROM source_item WHERE external_id = ?) ORDER BY id",
            (case["message"]["id"],),
        )
    )
    assert [row["due_at"] for row in rows] == expect["due_at"], case["name"]
    assert [row["estimate_source"] for row in rows] == expect["estimate_source"], case["name"]


def test_relative_date_fixture_does_not_drift_with_the_calendar(
    conn, settings: Settings, boundary
) -> None:  # type: ignore[no-untyped-def]
    """Fixture 02 exists to catch a regression to clock-based resolution.

    The message is from 2026-07-10 and says "by Friday". The answer is 2026-07-17 and it
    stays 2026-07-17 in 2030. If this ever starts returning a date near the current week,
    CLAUDE.md rule 4 has been broken.
    """
    case = load(FIXTURES / "commitments" / "02-relative-date-old-message.json")
    ledger = Ledger(conn, settings)
    run_case(case, ledger, settings, boundary)
    row = conn.execute("SELECT due_at FROM commitment").fetchone()
    assert row["due_at"] == "2026-07-17"


def test_superseded_row_is_tombstoned_not_deleted(conn, settings: Settings, boundary) -> None:  # type: ignore[no-untyped-def]
    """docs/03 §Retention: "Tombstone rather than delete ... so a resolved item cannot be
    resurrected by a later re-extraction of the same source." """
    ledger = Ledger(conn, settings)
    run_case(
        load(FIXTURES / "commitments" / "01-explicit-date.json"), ledger, settings, boundary
    )
    run_case(
        load(FIXTURES / "commitments" / "05-resolves-earlier.json"), ledger, settings, boundary
    )

    rows = list(conn.execute("SELECT status, superseded_by FROM commitment ORDER BY id"))
    assert len(rows) == 2, "the original row must still exist"
    assert rows[0]["status"] == "superseded"
    assert rows[0]["superseded_by"] == 2
    assert rows[1]["status"] == "open"


def test_low_confidence_stays_open_but_is_flagged_for_review(
    conn, settings: Settings, boundary
) -> None:  # type: ignore[no-untyped-def]
    """CLAUDE.md rule 2: low-confidence extractions go to a review queue, never into the
    brief as fact. The row is still created — it is not discarded — but the acceptance
    query marks it, and Phase 2 must read that flag."""
    from backglass.db import query
    from backglass.ledger import USER_ID

    ledger = Ledger(conn, settings)
    run_case(load(FIXTURES / "commitments" / "03-hedged.json"), ledger, settings, boundary)

    rows = list(
        conn.execute(
            query("open_commitments"),
            {"user_id": USER_ID, "confidence_threshold": settings.confidence_threshold},
        )
    )
    assert len(rows) == 1
    assert rows[0]["confidence"] == 0.5
    assert rows[0]["needs_review"] == 1


def test_every_open_commitment_carries_its_source(conn, settings: Settings, boundary) -> None:  # type: ignore[no-untyped-def]
    """CLAUDE.md rule 1. The acceptance query cannot return a row without provenance."""
    from backglass.db import query
    from backglass.ledger import USER_ID

    ledger = Ledger(conn, settings)
    for path in CASES:
        case = load(path)
        if case.get("requires"):
            continue
        run_case(case, ledger, settings, boundary)

    rows = list(
        conn.execute(
            query("open_commitments"),
            {"user_id": USER_ID, "confidence_threshold": settings.confidence_threshold},
        )
    )
    assert rows, "the fixture set should produce at least one open commitment"
    for row in rows:
        assert row["source_item_id"]
        assert row["source"] and row["source_external_id"]
        assert row["source_occurred_at"]


def test_a_backfill_extracts_oldest_first_so_supersession_can_fire(
    conn, settings: Settings, boundary
) -> None:  # type: ignore[no-untyped-def]
    """Regression: post-processing steps 4 and 5 are order dependent.

    Fixture 01 makes the promise on 2026-07-10; fixture 05 delivers it on 2026-07-15. A
    first-run backfill ingests both at once, so the order the extractor sees them in is
    decided by db/queries/pending_extraction.sql alone. Newest-first meant the resolution
    was extracted before the promise: there was nothing to supersede, and the promise was
    then deduped away against the resolution. The ledger ended up with a single open row
    citing the wrong source and no tombstone at all — a commitment that looks resolved
    and one that looks like it was never made.
    """
    from backglass.db import query
    from backglass.extract import prompts
    from backglass.ledger import USER_ID
    from backglass.sync import EXTRACT_PROMPT

    ledger = Ledger(conn, settings)
    for name in ("01-explicit-date.json", "05-resolves-earlier.json"):
        ingest(load(FIXTURES / "commitments" / name), ledger, boundary)
    for row in conn.execute("SELECT id FROM source_item"):
        ledger.record_triage(int(row["id"]), "keep", "fixture")

    pending = conn.execute(
        query("pending_extraction"),
        {"user_id": USER_ID, "compatible_versions": ",".join(prompts.load(EXTRACT_PROMPT).stamps)},
    ).fetchall()

    assert [row["external_id"] for row in pending] == ["c01", "c05"], (
        "the promise must be extracted before the delivery that resolves it"
    )
