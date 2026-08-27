"""Two plans that look like one — detected here, merged only by the owner.

`extract/engagements._same_row` refuses to merge two sightings whose wording differs, and
four rounds of verification stand behind the reason: a duplicate is visible on the board
and can be dismissed, while a wrongly merged plan silently replaces one the owner had
already agreed to.

Measured on the owner's ledger on 2026-08-24, that trade-off had cost 127 same-day
near-duplicate plans — 120 of them below the wording threshold, the matcher working
exactly as designed — and there was nowhere to dismiss a single one. The compensation the
trade-off assumes had never been built. This is it.
"""

from __future__ import annotations

import itertools
import sqlite3
from datetime import date

import pytest

from backglass import scrub
from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID
from backglass.web import actions

TODAY = date(2026, 8, 24)


@pytest.fixture
def sett(settings: Settings) -> Settings:
    return settings.model_copy(update={"default_tz": "America/Phoenix"})


_seq = itertools.count(1)


def a_plan(
    conn: sqlite3.Connection,
    what: str,
    *,
    starts_at: str | None = "2026-08-28T09:30:00",
    status: str = "proposed",
) -> int:
    # A counter rather than the title: two sightings of one plan is the whole subject
    # here, and they legitimately arrive on two different messages with the same words.
    n = next(_seq)
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, content_hash, triage_verdict)"
        " VALUES (?, 'apple-mail', ?, ?, '2026-08-01T09:00:00-07:00', ?, 'b', ?, 'keep')",
        (USER_ID, f"m-{n}", now_iso(), what, f"h-{n}"),
    )
    sid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO engagement (user_id, kind, what, starts_at, when_is_explicit,"
        " status, confidence, source_item_id, created_at)"
        " VALUES (?, 'social', ?, ?, 1, ?, 0.8, ?, ?)",
        (USER_ID, what, starts_at, status, sid, now_iso()),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


class TestFindingThem:
    def test_two_alike_plans_on_one_day_are_offered_as_a_pair(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        first = a_plan(conn, "Pih ball meetup", starts_at="2026-08-28T19:30:00")
        second = a_plan(conn, "Pih ball meetup", starts_at="2026-08-28T19:40:00")

        pairs, total = scrub.duplicate_plans(conn, sett)

        assert total == 1
        assert (pairs[0].a_id, pairs[0].b_id) == (min(first, second), max(first, second))
        assert pairs[0].a_when == "19:30" and pairs[0].b_when == "19:40"

    def test_the_same_words_on_different_days_are_two_plans(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """A standing arrangement is not a duplicate. Weekly pickleball with the same
        friend is a new plan each week, and collapsing them would delete the series."""
        a_plan(conn, "pickleball", starts_at="2026-08-25T19:00:00")
        a_plan(conn, "pickleball", starts_at="2026-09-01T19:00:00")

        _, total = scrub.duplicate_plans(conn, sett)

        assert total == 0

    def test_two_unrelated_plans_on_one_day_are_not_a_pair(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        a_plan(conn, "dentist appointment", starts_at="2026-08-28T09:30:00")
        a_plan(conn, "pickleball with Ashwin", starts_at="2026-08-28T19:00:00")

        _, total = scrub.duplicate_plans(conn, sett)

        assert total == 0

    def test_a_plan_that_already_happened_is_not_offered(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """`done` and `superseded` stay out for the reason `open_engagements` gives: a
        plan that happened and is arranged again is a new plan."""
        a_plan(conn, "Pih ball meetup", starts_at="2026-08-28T19:30:00", status="done")
        a_plan(conn, "Pih ball meetup", starts_at="2026-08-28T19:40:00")

        _, total = scrub.duplicate_plans(conn, sett)

        assert total == 0

    def test_a_pair_the_owner_kept_apart_is_never_offered_again(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """An unremembered "no" re-surfaces every morning forever, and a board that keeps
        showing rows the owner has cleared is a board they stop opening."""
        first = a_plan(conn, "Pih ball meetup", starts_at="2026-08-28T19:30:00")
        second = a_plan(conn, "Pih ball meetup", starts_at="2026-08-28T19:40:00")
        actions.different_plans(conn, first, second)

        _, total = scrub.duplicate_plans(conn, sett)

        assert total == 0


class TestMerging:
    def test_the_older_row_is_kept_and_the_newer_folded_into_it(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        del sett
        first = a_plan(conn, "McKenna kickoff dinner", starts_at="2026-08-28T18:00:00")
        second = a_plan(conn, "McKenna Program kickoff dinner", starts_at="2026-08-28T18:00:00")

        actions.same_plan(conn, first, second)

        rows = {
            int(r["id"]): r
            for r in conn.execute("SELECT id, status, superseded_by FROM engagement")
        }
        assert rows[first]["status"] == "proposed"
        assert rows[second]["status"] == "superseded"
        assert rows[second]["superseded_by"] == first

    def test_the_winners_hour_is_never_repainted(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """A commitment merge takes the earlier due date, because a deadline is a fact and
        the safer one is true. A plan's hour is a decision somebody made, and the two rows
        exist precisely because nothing said which hour replaced the other — picking one
        here would be the silent repaint the matcher refuses to make."""
        del sett
        first = a_plan(conn, "Pih ball meetup", starts_at="2026-08-28T19:30:00")
        second = a_plan(conn, "Pih ball meetup", starts_at="2026-08-28T19:40:00")

        actions.same_plan(conn, first, second)

        kept = conn.execute(
            "SELECT starts_at FROM engagement WHERE id = ?", (first,)
        ).fetchone()
        assert str(kept["starts_at"]) == "2026-08-28T19:30:00"

    def test_the_evidence_and_the_guests_travel(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The message that created the loser may be the only one that named somebody;
        losing it would make the merge cost information."""
        del sett
        first = a_plan(conn, "Pih ball meetup", starts_at="2026-08-28T19:30:00")
        second = a_plan(conn, "Pih ball meetup", starts_at="2026-08-28T19:40:00")
        loser_item = conn.execute(
            "SELECT source_item_id AS s FROM engagement WHERE id = ?", (second,)
        ).fetchone()["s"]
        conn.execute(
            "INSERT INTO engagement_evidence (user_id, engagement_id, source_item_id,"
            " quote, kind, seen_at) VALUES (?, ?, ?, 'see you at 7:40', 'original', ?)",
            (USER_ID, second, loser_item, now_iso()),
        )
        conn.execute(
            "INSERT INTO entity (user_id, kind, canonical_name) VALUES (?, 'person', 'Ashwin')",
            (USER_ID,),
        )
        entity_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO engagement_person (user_id, engagement_id, entity_id)"
            " VALUES (?, ?, ?)",
            (USER_ID, second, entity_id),
        )

        actions.same_plan(conn, first, second)

        assert conn.execute(
            "SELECT COUNT(*) AS n FROM engagement_evidence WHERE engagement_id = ?",
            (first,),
        ).fetchone()["n"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM engagement_person WHERE engagement_id = ?",
            (first,),
        ).fetchone()["n"] == 1

    def test_a_plan_that_is_no_longer_open_cannot_be_merged(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        del sett
        first = a_plan(conn, "Pih ball meetup", starts_at="2026-08-28T19:30:00")
        second = a_plan(conn, "Pih ball meetup", starts_at="2026-08-28T19:40:00", status="done")

        with pytest.raises(actions.ActionError):
            actions.same_plan(conn, first, second)

    def test_marking_one_pair_apart_twice_says_so(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        del sett
        first = a_plan(conn, "Pih ball meetup", starts_at="2026-08-28T19:30:00")
        second = a_plan(conn, "Pih ball meetup", starts_at="2026-08-28T19:40:00")
        actions.different_plans(conn, first, second)

        with pytest.raises(actions.ActionError):
            actions.different_plans(conn, second, first)


class TestThePage:
    def test_the_pairs_reach_the_scrub_board(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from fastapi.testclient import TestClient

        from backglass.web.app import create_app
        from tests.conftest import panel_slice

        a_plan(conn, "Pih ball meetup", starts_at="2026-08-28T19:30:00")
        a_plan(conn, "Pih ball meetup", starts_at="2026-08-28T19:40:00")
        conn.commit()

        client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")
        body = client.get("/scrub").text

        panel = panel_slice(body, "panel-scrub-plans")
        assert "Pih ball meetup" in panel
        assert "Same plan" in panel and "Both real" in panel

    def test_a_board_with_only_plan_pairs_is_not_reported_as_clear(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The empty state gates on the commitment count; a page that said "nothing to
        clear" above a list of duplicate plans would be lying in both directions."""
        from fastapi.testclient import TestClient

        from backglass.web.app import create_app

        a_plan(conn, "Pih ball meetup", starts_at="2026-08-28T19:30:00")
        a_plan(conn, "Pih ball meetup", starts_at="2026-08-28T19:40:00")
        conn.commit()

        client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")
        body = client.get("/scrub").text

        assert "Nothing to clear" not in body
