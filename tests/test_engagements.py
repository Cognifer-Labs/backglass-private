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


class TestDeclinedStaysDeclined:
    def test_re_extraction_does_not_resurrect_a_cancelled_plan(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """A prompt version bump re-reads the whole ledger (pending_extraction.sql), so
        the message that proposed a plan is read again after the message that cancelled
        it. While `declined` was hidden from the dedup query, that second reading matched
        nothing and filed a fresh `proposed` row — and the brief asked the owner to reply
        to a dinner they had already called off.
        """
        first, at_one = ingest(ledger := Ledger(conn, settings), boundary)
        run(ledger, settings, first, at_one, engagement())

        second, at_two = ingest(ledger, boundary, sent="2026-07-15T10:00:00-07:00")
        run(ledger, settings, second, at_two, engagement(status="declined"))
        assert [r["status"] for r in rows(conn)] == ["declined"]

        # …now re-extract the original message, exactly as a version bump would.
        report = run(ledger, settings, first, at_one, engagement())

        assert report.inserted == 0
        assert [r["status"] for r in rows(conn)] == ["declined"]

    def test_a_declined_plan_is_not_offered_for_review_or_the_brief(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        ledger = Ledger(conn, settings)
        item_id, occurred_at = ingest(ledger, boundary)
        run(ledger, settings, item_id, occurred_at, engagement(status="declined"))
        live = conn.execute(
            "SELECT COUNT(*) AS n FROM engagement WHERE status IN ('proposed','confirmed')"
        ).fetchone()
        assert live["n"] == 0


class TestSameDayDifferentHours:
    def test_two_plans_on_one_day_at_different_times_stay_two(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """Matching on the day alone fused them and kept only the first — and because
        `advance_engagement` fills holes rather than repainting, the losing hour was not
        even recorded anywhere. The 4pm coffee vanished from the ledger, the day plan and
        the brief."""
        ledger = Ledger(conn, settings)
        item_id, occurred_at = ingest(ledger, boundary)

        report = run(
            ledger,
            settings,
            item_id,
            occurred_at,
            engagement(what="coffee with Priya", starts_at="Friday at 9am"),
            engagement(what="coffee with Priya", starts_at="Friday at 4pm"),
        )

        assert report.inserted == 2
        assert sorted(str(r["starts_at"])[11:16] for r in rows(conn)) == ["09:00", "16:00"]

    def test_an_undated_plan_still_matches_the_message_that_times_it(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """The fallback the hour comparison must not break: when only one side names an
        hour, the day still carries the match."""
        ledger = Ledger(conn, settings)
        first, at_one = ingest(ledger, boundary)
        run(ledger, settings, first, at_one, engagement(starts_at="Friday"))

        second, at_two = ingest(ledger, boundary, sent="2026-07-15T10:00:00-07:00")
        report = run(ledger, settings, second, at_two, engagement(starts_at="Friday at 7pm"))

        assert report.inserted == 0
        assert len(rows(conn)) == 1


class TestReschedule:
    def test_a_later_message_moving_the_time_moves_the_plan(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """The most common follow-up a plan ever gets.

        Separating on the clock across messages turned "can we push dinner to 7:30" into
        a second row: two overlapping fixed blocks on the day, and two byte-identical
        lines in the brief, which does not print the hour — so the owner could not even
        tell which was stale, and nothing can delete a plan.
        """
        ledger = Ledger(conn, settings)
        first, at_one = ingest(ledger, boundary)
        run(ledger, settings, first, at_one, engagement(starts_at="Friday at 7pm"))

        second, at_two = ingest(ledger, boundary, sent="2026-07-15T10:00:00-07:00")
        report = run(
            ledger,
            settings,
            second,
            at_two,
            engagement(starts_at="Friday at 7:30pm", replaces_earlier=True),
        )

        assert report.inserted == 0
        assert report.advanced == 1
        (row,) = rows(conn)
        assert str(row["starts_at"]) == "2026-07-17T19:30:00"

    def test_two_times_in_one_message_are_still_two_plans(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """The other side of the same rule, and the reason it cannot simply match on the
        day: one message offering two times describes two plans, and the row the first
        candidate just wrote is already in the ledger when the second is matched."""
        ledger = Ledger(conn, settings)
        item_id, occurred_at = ingest(ledger, boundary)
        report = run(
            ledger,
            settings,
            item_id,
            occurred_at,
            engagement(what="coffee", starts_at="Friday at 9am", location=None),
            engagement(what="coffee", starts_at="Friday at 4pm", location=None),
        )
        assert report.inserted == 2
        assert sorted(str(r["starts_at"])[11:16] for r in rows(conn)) == ["09:00", "16:00"]

    def test_a_vaguer_later_mention_cannot_blank_a_known_hour(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """Repainting must not become erosion: a message that names only the day has to
        leave an hour an earlier message established alone."""
        ledger = Ledger(conn, settings)
        first, at_one = ingest(ledger, boundary)
        run(ledger, settings, first, at_one, engagement(starts_at="Friday at 7pm"))

        second, at_two = ingest(ledger, boundary, sent="2026-07-15T10:00:00-07:00")
        run(ledger, settings, second, at_two, engagement(starts_at="Friday"))

        (row,) = rows(conn)
        assert str(row["starts_at"]) == "2026-07-17T19:00:00"


class TestDeclinedIsNotASink:
    def test_a_genuinely_new_invitation_is_a_new_plan(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """Making declined rows visible to dedup stopped re-extraction resurrecting a
        cancelled plan — and, until this, made the dead row swallow real invitations
        forever. An undated re-invitation months later matched the corpse, was cited onto
        it, was never reopened, and reached no surface at all.

        The distinction is whether this exact message has been read against this row
        before: that is re-extraction, and anything else is someone asking again.
        """
        ledger = Ledger(conn, settings)
        first, at_one = ingest(ledger, boundary)
        run(ledger, settings, first, at_one, engagement(starts_at=None))
        second, at_two = ingest(ledger, boundary, sent="2026-07-15T10:00:00-07:00")
        run(ledger, settings, second, at_two, engagement(starts_at=None, status="declined"))
        assert [r["status"] for r in rows(conn)] == ["declined"]

        # Two months later, someone asks again.
        third, at_three = ingest(ledger, boundary, sent="2026-09-20T09:00:00-07:00")
        report = run(ledger, settings, third, at_three, engagement(starts_at=None))

        assert report.inserted == 1
        assert sorted(str(r["status"]) for r in rows(conn)) == ["declined", "proposed"]

    def test_re_extracting_a_message_it_already_cites_still_writes_nothing(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """The property the declined-visibility fix exists for, kept intact by the one
        above it."""
        ledger = Ledger(conn, settings)
        first, at_one = ingest(ledger, boundary)
        run(ledger, settings, first, at_one, engagement(starts_at=None))
        second, at_two = ingest(ledger, boundary, sent="2026-07-15T10:00:00-07:00")
        run(ledger, settings, second, at_two, engagement(starts_at=None, status="declined"))

        report = run(ledger, settings, first, at_one, engagement(starts_at=None))

        assert report.inserted == 0
        assert [r["status"] for r in rows(conn)] == ["declined"]


class TestMatchThenMatch:
    """The path three rounds of tests missed: a candidate that DEDUPES rather than
    inserting. Every earlier test exercised match-then-insert or insert-then-insert, so
    the same-response bookkeeping was only ever proven on rows this response created."""

    def test_a_second_option_survives_when_the_first_matched_an_existing_plan(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """"Coffee Friday 9am", then "coffee Friday — 9am or 4pm?".

        The first candidate matches the stored 9am plan and the second must become a new
        row. Because a dedup hit never joined the same-response set, the guard that keeps
        the second candidate off the first one's row was empty exactly when it was needed:
        both deduped onto the stored plan, the 4pm coffee was never written at all, and
        the 9am one was repainted to 16:00.
        """
        ledger = Ledger(conn, settings)
        first, at_one = ingest(ledger, boundary)
        run(
            ledger,
            settings,
            first,
            at_one,
            engagement(what="coffee", starts_at="Friday at 9am"),
        )

        second, at_two = ingest(ledger, boundary, sent="2026-07-15T10:00:00-07:00")
        report = run(
            ledger,
            settings,
            second,
            at_two,
            engagement(what="coffee", starts_at="Friday at 9am"),
            engagement(what="coffee", starts_at="Friday at 4pm"),
        )

        assert report.inserted == 1
        assert sorted(str(r["starts_at"])[11:16] for r in rows(conn)) == ["09:00", "16:00"]

    def test_re_extracting_two_same_day_plans_leaves_both_alone(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """A prompt-version bump re-reads every item, and both candidates then dedupe.
        Collapsing them lost the 9am plan and left two identical 4pm rows — with nothing
        in the system able to delete either."""
        ledger = Ledger(conn, settings)
        item_id, occurred_at = ingest(ledger, boundary)
        both = (
            engagement(what="coffee", starts_at="Friday at 9am"),
            engagement(what="coffee", starts_at="Friday at 4pm"),
        )
        run(ledger, settings, item_id, occurred_at, *both)
        before = [(r["id"], r["starts_at"]) for r in rows(conn)]

        second = Ledger(conn, settings)
        run(second, settings, item_id, occurred_at, *both)

        assert [(r["id"], r["starts_at"]) for r in rows(conn)] == before
        assert second.writes == 0

    def test_a_later_message_settles_the_option_it_names_not_the_first_one(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """With two plans agreeing on wording, people and day, "agreeing" cannot pick
        one. Taking the first row in id order repainted the already-confirmed 9am coffee
        when a later message settled the 4pm one, and left the 4pm row an unreachable
        duplicate."""
        ledger = Ledger(conn, settings)
        first, at_one = ingest(ledger, boundary)
        run(
            ledger,
            settings,
            first,
            at_one,
            engagement(what="coffee", starts_at="Friday at 9am"),
            engagement(what="coffee", starts_at="Friday at 4pm"),
        )

        second, at_two = ingest(ledger, boundary, sent="2026-07-15T10:00:00-07:00")
        run(
            ledger,
            settings,
            second,
            at_two,
            engagement(what="coffee", starts_at="Friday at 9am", status="confirmed"),
        )
        third, at_three = ingest(ledger, boundary, sent="2026-07-16T10:00:00-07:00")
        run(
            ledger,
            settings,
            third,
            at_three,
            engagement(what="coffee", starts_at="Friday at 4pm", status="confirmed"),
        )

        settled = sorted((str(r["starts_at"])[11:16], str(r["status"])) for r in rows(conn))
        assert settled == [("09:00", "confirmed"), ("16:00", "confirmed")]


class TestOlderMessagesDoNotOverwriteNewer:
    def test_replaying_the_message_a_reschedule_superseded_changes_nothing(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """Re-extraction replays messages in the order they were sent, so the "Friday at
        7" that a "push it to 7:30" corrected gets read again. Without a recency check the
        row ping-ponged on every version bump — the final state was right, but each pass
        wrote twice and any read between them saw the superseded time."""
        ledger = Ledger(conn, settings)
        first, at_one = ingest(ledger, boundary)
        run(ledger, settings, first, at_one, engagement(starts_at="Friday at 7pm"))
        second, at_two = ingest(ledger, boundary, sent="2026-07-15T10:00:00-07:00")
        run(
            ledger,
            settings,
            second,
            at_two,
            engagement(starts_at="Friday at 7:30pm", replaces_earlier=True),
        )

        replay = Ledger(conn, settings)
        run(replay, settings, first, at_one, engagement(starts_at="Friday at 7pm"))

        (row,) = rows(conn)
        assert str(row["starts_at"]) == "2026-07-17T19:30:00"
        assert replay.writes == 0


class TestOnlyAMoveMoves:
    """A differing clock time means a different plan unless the message says otherwise.

    Four verification rounds established that the times alone cannot answer this.
    Comparing them turned every reschedule into a second row that double-booked the day;
    ignoring them let a 4pm plan repaint an unrelated 9am one out of existence. The model
    now reports `replaces_earlier`, and when it is absent the answer is "different plan" —
    a duplicate is visible and dismissible, a wrongly merged plan is silent data loss.
    """

    def test_the_order_options_arrive_in_cannot_move_a_confirmed_plan(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """The ledger holds a confirmed 9am coffee; a new message offers "4pm or 9am" and
        the model happens to emit the 4pm first. That ordering used to repaint the
        confirmed plan to a time nobody had agreed to, while the 9am arrived as a new
        proposal — the same two options in the other order behaved correctly, which is
        what made it invisible."""
        ledger = Ledger(conn, settings)
        first, at_one = ingest(ledger, boundary)
        run(
            ledger,
            settings,
            first,
            at_one,
            engagement(what="coffee", starts_at="Friday at 9am", status="confirmed"),
        )

        second, at_two = ingest(ledger, boundary, sent="2026-07-15T10:00:00-07:00")
        run(
            ledger,
            settings,
            second,
            at_two,
            engagement(what="coffee", starts_at="Friday at 4pm"),
            engagement(what="coffee", starts_at="Friday at 9am"),
        )

        settled = sorted((str(r["starts_at"])[11:16], str(r["status"])) for r in rows(conn))
        assert settled == [("09:00", "confirmed"), ("16:00", "proposed")]

    def test_a_re_invitation_cannot_land_on_an_unrelated_live_plan(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """With a declined 4pm and a live 9am, a fresh 4pm invitation was correctly kept
        off the declined row by the citation guard — and then landed on the 9am one,
        because nothing rejected a poor match. The 9am plan ceased to exist."""
        ledger = Ledger(conn, settings)
        first, at_one = ingest(ledger, boundary)
        run(
            ledger,
            settings,
            first,
            at_one,
            engagement(what="coffee", starts_at="Friday at 9am"),
            engagement(what="coffee", starts_at="Friday at 4pm"),
        )
        second, at_two = ingest(ledger, boundary, sent="2026-07-15T10:00:00-07:00")
        run(
            ledger,
            settings,
            second,
            at_two,
            engagement(what="coffee", starts_at="Friday at 4pm", status="declined"),
        )

        third, at_three = ingest(ledger, boundary, sent="2026-07-20T09:00:00-07:00")
        run(
            ledger,
            settings,
            third,
            at_three,
            engagement(what="coffee", starts_at="Friday at 4pm"),
        )

        assert any(str(r["starts_at"]).endswith("09:00:00") for r in rows(conn)), (
            "the unrelated 9am plan must survive"
        )

    def test_declining_and_replacing_in_one_message_leaves_others_alone(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """"4pm is off, let's do 3:30" — the decline puts the 4pm row beyond reach of the
        second candidate, which then had nothing to land on but an unrelated 9am."""
        ledger = Ledger(conn, settings)
        first, at_one = ingest(ledger, boundary)
        run(
            ledger,
            settings,
            first,
            at_one,
            engagement(what="coffee", starts_at="Friday at 9am"),
            engagement(what="coffee", starts_at="Friday at 4pm"),
        )

        second, at_two = ingest(ledger, boundary, sent="2026-07-15T10:00:00-07:00")
        run(
            ledger,
            settings,
            second,
            at_two,
            engagement(what="coffee", starts_at="Friday at 4pm", status="declined"),
            engagement(what="coffee", starts_at="Friday at 3:30pm"),
        )

        settled = sorted((str(r["starts_at"])[11:16], str(r["status"])) for r in rows(conn))
        assert settled == [
            ("09:00", "proposed"),
            ("15:30", "proposed"),
            ("16:00", "declined"),
        ]

    def test_a_move_to_another_day_is_still_the_same_plan(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """The limit that used to be documented as unfixable. "Let's push it to Saturday"
        is one dinner; only a message that says it is moving something gets to cross a
        day boundary, so a weekly standing arrangement still stays separate."""
        ledger = Ledger(conn, settings)
        first, at_one = ingest(ledger, boundary)
        run(ledger, settings, first, at_one, engagement(starts_at="Friday at 7pm"))

        second, at_two = ingest(ledger, boundary, sent="2026-07-15T10:00:00-07:00")
        report = run(
            ledger,
            settings,
            second,
            at_two,
            engagement(starts_at="Saturday at 7pm", replaces_earlier=True),
        )

        assert report.inserted == 0
        (row,) = rows(conn)
        assert str(row["starts_at"]).startswith("2026-07-18")

    def test_a_weekly_arrangement_is_not_swallowed_by_a_move(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """The reason `replaces_earlier` gates the cross-day match instead of it being
        the default: without the flag, next week's coffee is a new plan."""
        ledger = Ledger(conn, settings)
        first, at_one = ingest(ledger, boundary)
        run(ledger, settings, first, at_one, engagement(what="coffee", starts_at="Friday"))

        second, at_two = ingest(ledger, boundary, sent="2026-07-21T09:00:00-07:00")
        report = run(
            ledger,
            settings,
            second,
            at_two,
            engagement(what="coffee", starts_at="Friday"),
        )

        assert report.inserted == 1
        assert len(rows(conn)) == 2


class TestNewestCitationAcrossTimezones:
    def test_the_newest_citation_is_chosen_by_instant_not_by_string(
        self, conn: Any, settings: Settings, boundary: Any
    ) -> None:
        """`MAX(occurred_at)` in SQL is a string comparison, and these timestamps carry
        each sender's own offset — so across the owner's UTC-7 / UTC+5:30 split
        "2026-07-16T01:00+05:30" sorts above "2026-07-15T20:00-07:00" while being half a
        day earlier. Picking the wrong newest citation lets a stale message repaint a
        time a later one corrected. Needs two prior citations with mixed offsets, which
        is why a single-citation test could not see it.
        """
        ledger = Ledger(conn, settings)

        def source(external_id: str, occurred_at: str) -> int:
            """Written straight in, because the Gmail connector normalises occurred_at to
            UTC and would erase the very mixture under test. The calendar connector does
            not — docs/03 keeps each event's own offset — and the owner's live ledger
            holds `-07:00` rows today, so the mixture is real."""
            conn.execute(
                "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
                " occurred_at, content_hash) VALUES (1, 'calendar:asu', ?, ?, ?, ?)",
                (external_id, occurred_at, occurred_at, f"h-{external_id}"),
            )
            return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

        kolkata = source("k1", "2026-07-16T01:00:00+05:30")  # 07-15 19:30Z
        phoenix = source("p1", "2026-07-15T20:00:00-07:00")  # 07-16 03:00Z — the newest
        later = source("k2", "2026-07-16T02:00:00+05:30")  # 07-15 20:30Z

        conn.execute(
            "INSERT INTO engagement (user_id, kind, what, starts_at, ends_at,"
            " when_is_explicit, location, status, confidence, source_item_id, created_at)"
            " VALUES (1, 'social', 'dinner', '2026-07-17T19:30:00', NULL, 1, NULL,"
            " 'proposed', 0.9, ?, '2026-07-15T00:00:00+00:00')",
            (kolkata,),
        )
        engagement_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        for source_id in (kolkata, phoenix):
            ledger.record_engagement_evidence(engagement_id, source_id, "q", kind="original")

        newest = ledger.newest_citation_before(engagement_id, later)

        assert newest == "2026-07-15T20:00:00-07:00"
