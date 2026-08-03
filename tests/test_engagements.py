"""Post-processing for engagements — the plans people make with each other.

Pipeline tests, not evals, on the same terms as tests/test_commitments.py: the model
response is canned, and what is under test is the deterministic half — entity resolution
over a guest list, date resolution against the message (rule 4), the dedup-and-advance
pass that turns "dinner Friday?" into a confirmed plan, the confidence gate (rule 2), and
zero writes on a re-read (rule 3).

Every case drives `apply()` over a source item that went through a real connector, so
`occurred_at` is a real timestamp rather than one the test chose to be convenient.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from backglass.config import Settings
from backglass.extract.engagements import apply
from backglass.extract.schemas import CommitmentExtraction
from backglass.ledger import Ledger
from tests.conftest import gmail_message, make_connector

# A Tuesday, so "Friday" resolves forward inside the same week.
SENT = "2026-07-14T09:00:00-07:00"


def ingest(
    ledger: Ledger, boundary: Any, *, sent: str = SENT, body: str = ""
) -> tuple[int, str]:
    """One message through the real connector, so `occurred_at` is a real timestamp.

    The id is keyed to the send time because a source_item is unique on
    (source, external_id): two messages sharing an id would be one row, and a test
    meaning to show a second sighting would silently be re-reading the first.
    """
    epoch_ms = int(datetime.fromisoformat(sent).timestamp() * 1000)
    connector = make_connector(
        [
            gmail_message(
                {
                    "id": f"msg-{sent}",
                    "from": "Priya Raman <priya@example.com>",
                    "to": "owner@example.com",
                    "date": sent,
                    # The connector takes occurred_at from internalDate, so setting only
                    # the Date header would leave every case resolving against the
                    # fixture default and prove nothing about rule 4.
                    "internal_date": str(epoch_ms),
                    "subject": "dinner",
                    "body": body or "Are you free Friday?",
                }
            )
        ],
        boundary,
    )
    item = next(iter(connector.fetch(None)))
    item_id, _ = ledger.upsert_source_item(item)
    return item_id, item.occurred_at


def engagement(**overrides: Any) -> dict[str, Any]:
    base = {
        "kind": "social",
        "what": "dinner at Ravi's",
        "people": ["Priya Raman <priya@example.com>"],
        "starts_at": "Friday",
        "ends_at": None,
        "when_is_explicit": True,
        "location": "Ravi's on 5th",
        "status": "proposed",
        "confidence": 0.9,
        "evidence": "Are you free Friday for dinner at Ravi's?",
    }
    base.update(overrides)
    return base


def run(  # type: ignore[no-untyped-def]
    ledger: Ledger,
    settings: Settings,
    item_id: int,
    occurred_at: str,
    *plans: dict[str, Any],
):
    extraction = CommitmentExtraction.model_validate({"engagements": list(plans)})
    return apply(
        extraction,
        source_item_id=item_id,
        occurred_at=occurred_at,
        ledger=ledger,
        settings=settings,
    )


def rows(conn: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM engagement ORDER BY id")]


class TestInsert:
    def test_a_plan_becomes_a_row_with_its_people_and_its_sentence(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        ledger = Ledger(conn, settings)
        item_id, occurred_at = ingest(ledger, boundary)

        report = run(ledger, settings, item_id, occurred_at, engagement())

        assert report.inserted == 1
        (row,) = rows(conn)
        assert row["kind"] == "social"
        assert row["status"] == "proposed"
        assert row["location"] == "Ravi's on 5th"

        guests = [
            r["canonical_name"]
            for r in conn.execute(
                "SELECT e.canonical_name FROM engagement_person p "
                "JOIN entity e ON e.id = p.entity_id WHERE p.engagement_id = ?",
                (row["id"],),
            )
        ]
        assert guests == ["Priya Raman"]

        quote = conn.execute(
            "SELECT quote, kind FROM engagement_evidence WHERE engagement_id = ?",
            (row["id"],),
        ).fetchone()
        assert quote["quote"] == "Are you free Friday for dinner at Ravi's?"
        assert quote["kind"] == "original"

    def test_the_time_resolves_against_the_message_not_today(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """Rule 4, for starts_at as much as for a commitment's due_at.

        The message is sent on Tuesday 2026-07-14, so its "Friday" is 2026-07-17 — and
        stays 2026-07-17 no matter when the extraction runs.
        """
        ledger = Ledger(conn, settings)
        item_id, occurred_at = ingest(ledger, boundary)
        run(ledger, settings, item_id, occurred_at, engagement())
        (row,) = rows(conn)
        assert str(row["starts_at"]).startswith("2026-07-17")

    def test_a_plan_with_no_time_is_still_a_plan(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """"We should get dinner sometime" is the plan most likely to go stale, so it is
        the one it would hurt most to drop for lacking a timestamp."""
        ledger = Ledger(conn, settings)
        item_id, occurred_at = ingest(ledger, boundary)
        run(ledger, settings, item_id, occurred_at, engagement(starts_at=None))
        (row,) = rows(conn)
        assert row["starts_at"] is None
        assert row["status"] == "proposed"

    def test_a_plan_with_several_guests_is_one_row(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        ledger = Ledger(conn, settings)
        item_id, occurred_at = ingest(ledger, boundary)
        run(
            ledger,
            settings,
            item_id,
            occurred_at,
            engagement(people=["Priya Raman <priya@example.com>", "Sam Ellis"]),
        )
        assert len(rows(conn)) == 1
        count = conn.execute("SELECT COUNT(*) AS n FROM engagement_person").fetchone()["n"]
        assert count == 2


class TestConfidence:
    def test_a_low_confidence_plan_is_stored_but_queued_for_review(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """Rule 2: it exists, it just is not believed yet."""
        ledger = Ledger(conn, settings)
        item_id, occurred_at = ingest(ledger, boundary)
        report = run(ledger, settings, item_id, occurred_at, engagement(confidence=0.4))
        assert report.inserted == 1
        assert report.review_queue == 1


class TestAdvance:
    def test_a_reply_confirms_the_plan_it_answers_instead_of_making_a_second_one(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """The behaviour the whole record type exists for.

        "Dinner Friday?" then "Friday works" is one dinner. A commitment's dedup would
        record the restatement and drop it; a plan's has to move the row, because the
        second sentence is what makes it real.
        """
        ledger = Ledger(conn, settings)
        first, occurred_at = ingest(ledger, boundary)
        run(ledger, settings, first, occurred_at, engagement())

        second, second_at = ingest(
            ledger, boundary, sent="2026-07-15T10:00:00-07:00", body="Friday works!"
        )
        report = run(
            ledger,
            settings,
            second,
            second_at,
            engagement(status="confirmed", evidence="Friday works!"),
        )

        assert report.inserted == 0
        assert report.advanced == 1
        (row,) = rows(conn)
        assert row["status"] == "confirmed"

        cited = [
            (r["kind"], r["quote"])
            for r in conn.execute(
                "SELECT kind, quote FROM engagement_evidence "
                "WHERE engagement_id = ? ORDER BY id",
                (row["id"],),
            )
        ]
        assert cited == [
            ("original", "Are you free Friday for dinner at Ravi's?"),
            ("restated", "Friday works!"),
        ]

    def test_a_later_mention_cannot_walk_a_confirmed_plan_back_to_proposed(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """"Still on for Friday?" reads as a proposal and must not unconfirm the plan —
        that would drop a block off the day on the strength of a pleasantry."""
        ledger = Ledger(conn, settings)
        first, at = ingest(ledger, boundary)
        run(ledger, settings, first, at, engagement(status="confirmed"))

        second, second_at = ingest(ledger, boundary, sent="2026-07-16T08:00:00-07:00")
        run(ledger, settings, second, second_at, engagement(status="proposed"))

        (row,) = rows(conn)
        assert row["status"] == "confirmed"

    def test_a_cancellation_is_reachable_from_confirmed(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        ledger = Ledger(conn, settings)
        first, at = ingest(ledger, boundary)
        run(ledger, settings, first, at, engagement(status="confirmed"))

        second, second_at = ingest(ledger, boundary, sent="2026-07-16T08:00:00-07:00")
        run(ledger, settings, second, second_at, engagement(status="declined"))

        (row,) = rows(conn)
        assert row["status"] == "declined"

    def test_an_undated_plan_learns_its_date_from_the_message_that_sets_one(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        ledger = Ledger(conn, settings)
        first, at = ingest(ledger, boundary)
        run(ledger, settings, first, at, engagement(starts_at=None, location=None))

        second, second_at = ingest(ledger, boundary, sent="2026-07-15T10:00:00-07:00")
        run(ledger, settings, second, second_at, engagement(status="confirmed"))

        (row,) = rows(conn)
        assert str(row["starts_at"]).startswith("2026-07-17")
        assert row["location"] == "Ravi's on 5th"

    def test_a_low_confidence_restatement_is_cited_but_does_not_move_the_row(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """Changing an existing row is heavier than adding one: a wrong `declined` makes
        a real plan vanish. The sentence is still recorded so the owner can see it."""
        ledger = Ledger(conn, settings)
        first, at = ingest(ledger, boundary)
        run(ledger, settings, first, at, engagement())

        second, second_at = ingest(ledger, boundary, sent="2026-07-15T10:00:00-07:00")
        run(
            ledger,
            settings,
            second,
            second_at,
            engagement(status="declined", confidence=0.3, evidence="might have to bail"),
        )

        (row,) = rows(conn)
        assert row["status"] == "proposed"
        quotes = [
            r["quote"]
            for r in conn.execute(
                "SELECT quote FROM engagement_evidence WHERE engagement_id = ? ORDER BY id",
                (row["id"],),
            )
        ]
        assert "might have to bail" in quotes


class TestDistinctPlans:
    def test_two_different_plans_with_the_same_person_stay_two_rows(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """People alone must not fuse plans, or every arrangement with a close friend
        collapses into one row."""
        ledger = Ledger(conn, settings)
        item_id, occurred_at = ingest(ledger, boundary)
        run(
            ledger,
            settings,
            item_id,
            occurred_at,
            engagement(),
            engagement(what="squash on Sunday", starts_at="Sunday", location=None),
        )
        assert len(rows(conn)) == 2

    def test_the_same_wording_on_a_different_day_is_a_different_plan(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """A recurring arrangement is not one row. Weekly coffee with the same person is
        a new plan each week, and collapsing them would silently drop the later ones."""
        ledger = Ledger(conn, settings)
        first, at = ingest(ledger, boundary)
        run(ledger, settings, first, at, engagement(what="coffee", starts_at="Friday"))

        second, second_at = ingest(ledger, boundary, sent="2026-07-21T09:00:00-07:00")
        run(ledger, settings, second, second_at, engagement(what="coffee", starts_at="Friday"))

        assert len(rows(conn)) == 2


class TestIdempotency:
    def test_reading_the_same_message_twice_writes_nothing_the_second_time(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """Rule 3, asserted on `writes` rather than on row counts — an UPDATE that
        rewrites a row to the value it already holds is a write nobody needed."""
        ledger = Ledger(conn, settings)
        item_id, occurred_at = ingest(ledger, boundary)
        run(ledger, settings, item_id, occurred_at, engagement())

        second = Ledger(conn, settings)
        report = run(second, settings, item_id, occurred_at, engagement())

        assert report.inserted == 0
        assert second.writes == 0, "a re-read of an unchanged message must write nothing"


class TestOwner:
    def test_the_owner_is_not_a_guest_at_their_own_plan(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        ledger = Ledger(conn, settings)
        item_id, occurred_at = ingest(ledger, boundary)
        owner = f"{settings.owner_name} <{settings.owner_emails[0]}>"
        run(
            ledger,
            settings,
            item_id,
            occurred_at,
            engagement(people=[owner, "Priya Raman <priya@example.com>"]),
        )
        guests = [
            r["canonical_name"]
            for r in conn.execute(
                "SELECT e.canonical_name FROM engagement_person p "
                "JOIN entity e ON e.id = p.entity_id"
            )
        ]
        assert guests == ["Priya Raman"]


@pytest.mark.parametrize("kind", ["social", "professional"])
def test_both_kinds_round_trip(
    conn: Any, settings: Settings, boundary: Any, kind: str
) -> None:
    ledger = Ledger(conn, settings)
    item_id, occurred_at = ingest(ledger, boundary)
    run(ledger, settings, item_id, occurred_at, engagement(kind=kind))
    (row,) = rows(conn)
    assert row["kind"] == kind


class TestEndToEndThroughSync:
    """The wiring, not the appliers.

    Everything above drives `apply()` directly, which would keep passing if sync.py
    never called it — the exact shape tasks/lessons.md keeps recording, where a rule is
    proven at the function and the real door walks past it. This drives `sync()` and
    then the planner, so a plan found in a message ends up on the day.
    """

    def test_a_plan_in_a_message_becomes_a_block_on_the_day(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        from datetime import date as _date

        from backglass.plan import planner
        from backglass.sync import sync
        from tests.conftest import FakeModel

        message = gmail_message(
            {
                "id": "plan-e2e",
                "from": "Priya Raman <priya@example.com>",
                "to": "owner@example.com",
                "date": SENT,
                "internal_date": str(int(datetime.fromisoformat(SENT).timestamp() * 1000)),
                "subject": "Friday",
                "body": "Lunch at Ravi's on Friday at 1? Sam is in.",
            }
        )
        model = FakeModel(
            extract={
                "Lunch at Ravi's": {
                    "commitments": [],
                    "engagements": [
                        engagement(
                            what="lunch at Ravi's",
                            starts_at="Friday at 1pm",
                            status="confirmed",
                            people=["Priya Raman <priya@example.com>"],
                        )
                    ],
                }
            },
            triage={"Lunch at Ravi's": {"keep": True, "reason": "a plan"}},
        )

        report = sync(conn, settings, [make_connector([message], boundary)], model)

        assert report.engagements_inserted == 1, report
        row = conn.execute("SELECT * FROM engagement").fetchone()
        assert row["status"] == "confirmed"
        assert str(row["starts_at"]).startswith("2026-07-17T13:00")

        # …and the planner puts it on that day, with its location, as a fixed block.
        proposal = planner.propose(conn, settings, _date(2026, 7, 17))
        fixed = [b for b in proposal.blocks if b["kind"] == "fixed"]
        assert any("lunch at Ravi's" in str(b["title"]) for b in fixed), fixed

    def test_a_second_sync_over_the_same_message_writes_nothing(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """Rule 3 through the real pipeline, where the content hash short-circuits
        before the model is called at all."""
        from backglass.sync import sync
        from tests.conftest import FakeModel

        message = gmail_message(
            {
                "id": "plan-e2e-2",
                "from": "Priya Raman <priya@example.com>",
                "to": "owner@example.com",
                "date": SENT,
                "internal_date": str(int(datetime.fromisoformat(SENT).timestamp() * 1000)),
                "subject": "Friday",
                "body": "Dinner at Ravi's on Friday at 7?",
            }
        )
        model = FakeModel(
            extract={"Dinner at Ravi's": {"commitments": [], "engagements": [engagement()]}},
            triage={"Dinner at Ravi's": {"keep": True, "reason": "a plan"}},
        )
        sync(conn, settings, [make_connector([message], boundary)], model)
        second = sync(conn, settings, [make_connector([message], boundary)], model)

        assert second.writes == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM engagement").fetchone()["n"] == 1
