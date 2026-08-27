"""The conflict priority list — the owner's ruled order, applied and readable.

Owner, 2026-08-24: "there should be a priority list in terms of conflicts."

The list existed as behaviour before it existed as a list: fixed events won because
`capacity` carved them out, routines won because `day_events` placed them first, and
commitments tied because the fact the lane mechanism reads had never been written. These
tests are about the two halves that changes: the order is now a default rather than an
empty set, and a collision is *said* rather than resolved behind the owner's back.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID
from backglass.plan import planner, preferences, priority
from backglass.plan.capacity import FixedEvent

MONDAY = date(2026, 8, 24)
PHOENIX = "America/Phoenix"


@pytest.fixture
def sett(settings: Settings) -> Settings:
    return settings.model_copy(update={
        "default_tz": PHOENIX,
        "confidence_threshold": 0.7,
        "working_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    })


def a_commitment(
    conn: sqlite3.Connection, what: str, *, minutes: int = 45, due: str | None = None
) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, content_hash, triage_verdict)"
        " VALUES (?, 'manual', ?, '2026-08-20T00:00:00Z', '2026-08-20T00:00:00Z',"
        " ?, ?, ?, 'keep')",
        (USER_ID, f"x-{what}", what, what, f"h-{what}"),
    )
    sid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, confidence, status,"
        " estimated_minutes, estimate_source, source_item_id, created_at)"
        " VALUES (?, 'i_owe', ?, ?, 0.9, 'open', ?, 'manual', ?, ?)",
        (USER_ID, what, due, minutes, sid, now_iso()),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


class TestTheListItself:
    def test_it_is_the_owners_ruled_order(self) -> None:
        """Pinned deliberately. This is a *decision*, not a derived value, and a test
        that re-derives it from the code it is checking would assert nothing."""
        assert [tier.rank for tier in priority.TIERS] == [1, 2, 3, 4, 5, 6, 7]
        assert "fixed class" in priority.TIERS[0].name
        assert "48h" in priority.TIERS[1].name
        assert priority.TIERS[2].name == "gym, meals, sleep"
        assert priority.TIERS[3].name == "homework due this week"
        assert priority.TIERS[4].name == "coding / OrgTruth"
        assert priority.TIERS[5].name == "clinical and premed admin"
        assert priority.TIERS[6].name == "errands, email, social"

    def test_every_tier_says_how_it_actually_wins(self) -> None:
        """Three of the seven are enforced by the shape of the day rather than by any
        ranking, and a list that did not say so would be read as seven ranks."""
        assert all(tier.enforced_by for tier in priority.TIERS)

    def test_text_lands_in_the_tier_the_list_names(self) -> None:
        assert priority.rank_of("Take PSY101 Exam 4 via LockDown Browser") == 2
        assert priority.rank_of("Complete module 1-1-2 reading") == 4
        assert priority.rank_of("ship the OrgTruth deploy") == 5
        assert priority.rank_of("call the hospice volunteer coordinator") == 6
        assert priority.rank_of("water the plants") == 7, "unnamed things sort last"

    def test_matching_is_on_word_boundaries(self) -> None:
        """The 2026-07-30 banned-word lesson: a substring check on a short word fails the
        innocent case. "final" must not fire inside "finalise"."""
        assert priority.rank_of("finalise the housing paperwork") != 2


class TestItReachesThePlanner:
    def test_the_default_is_the_list_rather_than_nothing(
        self, conn: sqlite3.Connection
    ) -> None:
        """The half that had never once changed an outcome: `preferences.load` returned an
        empty set from August until this change, because the fact it reads was never
        written."""
        assert preferences.load(conn).lanes == priority.lanes().lanes
        assert preferences.load(conn).rank("Take PSY101 Exam 4") < preferences.load(
            conn
        ).rank("water the plants")

    def test_an_exam_outranks_an_errand_in_a_real_proposal(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Driven through `propose`, not `order` — the door callers use. Both are undated
        so the date bands cannot decide it; only the list can."""
        a_commitment(conn, "water the plants and tidy the desk", minutes=45)
        a_commitment(conn, "revise for the CHM 113 exam", minutes=45)

        proposal = planner.propose(conn, sett, MONDAY, events=[])

        work = [str(b["title"]) for b in proposal.blocks if b["kind"] in ("work", "protected")]
        assert work.index("revise for the CHM 113 exam") < work.index(
            "water the plants and tidy the desk"
        )

    def test_the_list_still_cannot_outrank_a_due_date(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The boundary the lane mechanism was built with and this does not move: an
        overdue errand is overdue whatever tier it lives in."""
        a_commitment(conn, "water the plants", minutes=45, due="2026-08-01")
        a_commitment(conn, "revise for the CHM 113 exam", minutes=45, due="2026-12-01")

        proposal = planner.propose(conn, sett, MONDAY, events=[])

        work = [str(b["title"]) for b in proposal.blocks if b["kind"] in ("work", "protected")]
        assert work[0] == "water the plants"


class TestACollisionIsSaidNotResolved:
    def _packed_day(self, conn: sqlite3.Connection, sett: Settings) -> planner.Proposal:
        zone = ZoneInfo(PHOENIX)

        def at_(h: int, m: int) -> datetime:
            return datetime.combine(MONDAY, time(h, m), tzinfo=zone)

        # Gaps of an hour or less, and a two-hour assignment due tomorrow that fits in
        # none of them. The errands do fit, so the day ends up holding lower-tier work
        # while the tier-2 item is in overflow — which is exactly the collision the list
        # exists to describe.
        events = [
            FixedEvent(title="class", starts_at=at_(*a), ends_at=at_(*b))
            for a, b in (((10, 0), (12, 0)), ((13, 0), (18, 0)))
        ]
        a_commitment(conn, "errand one", minutes=45)
        a_commitment(conn, "errand two", minutes=45)
        a_commitment(
            conn, "finish the CIS236 assignment", minutes=120,
            due=(MONDAY + timedelta(days=1)).isoformat(),
        )
        return planner.propose(
            conn,
            sett.model_copy(update={
                "daily_reserve_minutes": 1,
            }),
            MONDAY,
            events=events,
        )

    def test_the_plan_names_what_is_holding_the_time(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        proposal = self._packed_day(conn, sett)

        assert proposal.overflow, "precondition: something did not fit"
        note = next((n for n in proposal.notes if "priority list" in n), None)
        assert note is not None, (
            "a collision the owner cannot see is a collision they cannot fix"
        )
        assert "tier" in note
        assert "Nothing was moved" in note

    def test_no_routine_is_moved_to_make_room(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Tier 2 sits above tier 3, so the list *says* a paper due tomorrow outranks the
        gym. It does not act on it: a routine is the owner's own decision about their day,
        and a planner that deleted dinner to fit an essay is the surface nobody trusts
        twice."""
        proposal = self._packed_day(conn, sett)

        # The tier-2 item is in overflow and the tier-7 errands are still on the plan:
        # the list stated the collision and evicted nothing to resolve it.
        placed = {str(b["title"]) for b in proposal.blocks}
        assert {"errand one", "errand two"} <= placed
        assert "finish the CIS236 assignment" in {c.what for c in proposal.overflow}
        assert any("Nothing was moved" in n for n in proposal.notes)
