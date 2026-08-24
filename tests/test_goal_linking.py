"""The roadmap reaching the day's work, and refusing to reach where it cannot see.

The gap, measured on 2026-08-23: 8 goals, 4 roadmaps, 28 dated steps, 61 targets, five of
the eight goals at risk — and **one open commitment of 387 carrying a `goal_id`**. The
planner's whole at-risk tier had one row it could ever promote.

The negatives are the file, as in `test_relevance.py`, but the danger is different and so
is the balance. A wrong drop there disappears silently; a wrong link here shows up in
tomorrow's plan and undoes in a click. What must not happen is a link nobody can explain —
`commitment.goal_id` is a bare integer with no provenance of its own, so the quote check is
what keeps rule 1 true for a column that predates it.

The fixtures are the real failure that made this a model pass rather than a matcher:
matching commitment text against goal vocabulary linked 78 of 387 rows, and 72 came from
the single token `submit`.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from backglass.config import Settings
from backglass.extract import prompts
from backglass.goals import linking
from backglass.ledger import USER_ID


@pytest.fixture
def prompt():  # type: ignore[no-untyped-def]
    return prompts.load("link-goals")


def a_goal(conn: sqlite3.Connection, title: str, *, targets: tuple[str, ...] = ()) -> int:
    conn.execute(
        "INSERT INTO goal (user_id, title, status, horizon, definition_of_done,"
        " created_at) VALUES (?, ?, 'active', 'annual', ?, '2026-08-01T00:00:00Z')",
        (USER_ID, title, f"{title} — done"),
    )
    goal_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    for t in targets:
        conn.execute(
            "INSERT INTO target (goal_id, kind, title, active, created_at, user_id)"
            " VALUES (?, 'milestone', ?, 1, '2026-08-01T00:00:00Z', ?)",
            (goal_id, t, USER_ID),
        )
    return goal_id


def an_obligation(conn: sqlite3.Connection, what: str, *, n: int = 1) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, content_hash, triage_verdict)"
        " VALUES (?, 'manual', ?, '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z',"
        " 'someone@example.com', 'T', ?, ?, 'keep')",
        (USER_ID, f"g-{n}-{what[:20]}", what, f"h-{n}-{what[:20]}"),
    )
    item = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, confidence, status,"
        " source_item_id, created_at) VALUES (?, 'i_owe', ?, 0.9, 'open', ?,"
        " '2026-08-10T00:00:00Z')",
        (USER_ID, what, item),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def work(conn: sqlite3.Connection) -> linking.Work:
    return linking.Work(
        goals=linking.goals_with_targets(conn), commitments=linking.candidates(conn)
    )


def answer(**kwargs: Any) -> dict[str, Any]:
    base = {"commitment_id": 1, "goal_id": None, "confidence": 0.9,
            "quote": None, "reason": None}
    base.update(kwargs)
    return {"links": [base]}


class TestWhatReachesTheModel:
    def test_the_goals_carry_their_targets(self, conn: sqlite3.Connection, prompt) -> None:  # type: ignore[no-untyped-def]
        """Rule 3, and the reason a title-only prompt would fail. "Get into a competitive
        med school" contains none of the words that identify its work."""
        a_goal(conn, "Get into a competitive med school",
               targets=("Non-clinical volunteering hours", "Sit the MCAT"))
        an_obligation(conn, "Call the phone-only hospices about volunteering")

        _, dynamic = linking.render(work(conn), prompt=prompt)
        assert "Non-clinical volunteering hours" in dynamic
        assert "Sit the MCAT" in dynamic

    def test_every_instruction_sits_above_every_placeholder(self, prompt) -> None:  # type: ignore[no-untyped-def]
        """The caching rule `Prompt.split`'s docstring asks the next prompt to obey: a
        rule written below a placeholder is a rule paid for on every call for ever."""
        static, dynamic = prompt.split()
        assert len(static) > 2000
        assert "Rules:" in static
        assert dynamic.strip().endswith("{{commitments}}")


class TestTheGuards:
    def test_an_id_the_pass_never_sent_is_discarded(self, conn: sqlite3.Connection) -> None:
        """The tables overlap in range, so a plausible integer is not evidence
        (lessons, 2026-08-12)."""
        goal = a_goal(conn, "Ship OrgTruth to first users")
        an_obligation(conn, "OrgTruth: run e2e:live")

        links, discarded = linking.parse(
            answer(commitment_id=9999, goal_id=goal, quote="OrgTruth"), work(conn)
        )
        assert links == [] and discarded == 1

    def test_a_goal_that_was_not_sent_is_discarded(self, conn: sqlite3.Connection) -> None:
        a_goal(conn, "Ship OrgTruth to first users")
        cid = an_obligation(conn, "OrgTruth: run e2e:live")

        links, discarded = linking.parse(
            answer(commitment_id=cid, goal_id=4242, quote="OrgTruth"), work(conn)
        )
        assert links == [] and discarded == 1

    def test_a_link_with_no_quote_is_discarded(self, conn: sqlite3.Connection) -> None:
        """`commitment.goal_id` carries no provenance of its own. Without the quote a
        year-old link is a plan reprioritised for a reason nobody can reconstruct."""
        goal = a_goal(conn, "Ship OrgTruth to first users")
        cid = an_obligation(conn, "OrgTruth: run e2e:live")

        links, discarded = linking.parse(
            answer(commitment_id=cid, goal_id=goal, quote="   "), work(conn)
        )
        assert links == [] and discarded == 1

    def test_a_quote_not_in_the_obligation_is_discarded(
        self, conn: sqlite3.Connection
    ) -> None:
        """The guard against a fluent paraphrase standing in for having read the row."""
        goal = a_goal(conn, "Ship OrgTruth to first users")
        cid = an_obligation(conn, "OrgTruth: run e2e:live")

        links, discarded = linking.parse(
            answer(commitment_id=cid, goal_id=goal,
                   quote="ship the product to its first customers"),
            work(conn),
        )
        assert links == [] and discarded == 1

    def test_a_quoted_link_survives(self, conn: sqlite3.Connection) -> None:
        goal = a_goal(conn, "Ship OrgTruth to first users")
        cid = an_obligation(conn, "OrgTruth: run e2e:live against a real key")

        links, discarded = linking.parse(
            answer(commitment_id=cid, goal_id=goal, quote="OrgTruth: run e2e:live"),
            work(conn),
        )
        assert discarded == 0
        assert links[0].goal_id == goal

    def test_no_goal_needs_no_quote(self, conn: sqlite3.Connection) -> None:
        """The ordinary answer, and demanding evidence for it would push the model toward
        inventing links to avoid looking uncertain."""
        a_goal(conn, "Ship OrgTruth to first users")
        cid = an_obligation(conn, "Submit MMR immunization records")

        links, discarded = linking.parse(answer(commitment_id=cid), work(conn))
        assert discarded == 0
        assert links[0].goal_id is None


class TestWriting:
    def test_a_link_is_applied_and_recorded_with_its_cause(
        self, conn: sqlite3.Connection
    ) -> None:
        from backglass import claim_events

        goal = a_goal(conn, "Ship OrgTruth to first users")
        cid = an_obligation(conn, "OrgTruth: run e2e:live")
        linking.apply(conn, [linking.Link(cid, goal, 0.9, "OrgTruth", None)])

        row = conn.execute("SELECT goal_id FROM commitment WHERE id = ?", (cid,)).fetchone()
        assert row["goal_id"] == goal
        events = claim_events.since(conn, "commitment", cid)
        assert [(e["field"], e["cause"]) for e in events] == [("goal_id", "goal:linked")]

    def test_a_no_goal_verdict_is_recorded_too(self, conn: sqlite3.Connection) -> None:
        """What makes the next run skip the row. A pass that recorded only its positives
        would re-ask about the other three hundred every sync for ever."""
        a_goal(conn, "Ship OrgTruth to first users")
        cid = an_obligation(conn, "Submit MMR immunization records")
        linking.apply(conn, [linking.Link(cid, None, 0.4, None, None)])

        assert linking.candidates(conn) == []

    def test_a_row_resolved_since_the_call_went_out_is_not_written(
        self, conn: sqlite3.Connection
    ) -> None:
        """Re-read at write time (lessons, 2026-08-12): another pass may have closed it
        between the request and the answer."""
        goal = a_goal(conn, "Ship OrgTruth to first users")
        cid = an_obligation(conn, "OrgTruth: run e2e:live")
        conn.execute("UPDATE commitment SET status = 'done' WHERE id = ?", (cid,))

        linked, _ = linking.apply(conn, [linking.Link(cid, goal, 0.9, "OrgTruth", None)])
        assert linked == 0
        row = conn.execute("SELECT goal_id FROM commitment WHERE id = ?", (cid,)).fetchone()
        assert row["goal_id"] is None

    def test_a_goal_the_owner_set_by_hand_is_never_rejudged(
        self, conn: sqlite3.Connection
    ) -> None:
        """`candidates` skips anything already carrying a goal. A pass that re-judged what
        a person decided would be overwriting them."""
        goal = a_goal(conn, "Ship OrgTruth to first users")
        cid = an_obligation(conn, "OrgTruth: run e2e:live")
        conn.execute("UPDATE commitment SET goal_id = ? WHERE id = ?", (goal, cid))

        assert linking.candidates(conn) == []

    def test_a_dry_run_names_the_link_and_writes_none_of_it(
        self, conn: sqlite3.Connection
    ) -> None:
        goal = a_goal(conn, "Ship OrgTruth to first users")
        cid = an_obligation(conn, "OrgTruth: run e2e:live")

        linked, notes = linking.apply(
            conn, [linking.Link(cid, goal, 0.9, "OrgTruth", None)], dry_run=True
        )
        assert linked == 1 and notes
        row = conn.execute("SELECT goal_id FROM commitment WHERE id = ?", (cid,)).fetchone()
        assert row["goal_id"] is None
        assert linking.candidates(conn)  # still unjudged, not silently swallowed


class TestJudgedOnce:
    def test_a_second_pass_over_an_unchanged_ledger_sends_nothing(
        self, conn: sqlite3.Connection
    ) -> None:
        """Rule 3, and what keeps this quiet on a thirty-minute timer."""
        a_goal(conn, "Ship OrgTruth to first users")
        for i, what in enumerate(("OrgTruth: run e2e:live", "Submit MMR records")):
            an_obligation(conn, what, n=i)
        first = linking.candidates(conn)
        assert len(first) == 2

        linking.apply(conn, [linking.Link(c["id"], None, 0.5, None, None) for c in first])
        assert linking.candidates(conn) == []

    def test_new_work_is_still_picked_up(self, conn: sqlite3.Connection) -> None:
        a_goal(conn, "Ship OrgTruth to first users")
        first = an_obligation(conn, "OrgTruth: run e2e:live", n=1)
        linking.apply(conn, [linking.Link(first, None, 0.5, None, None)])

        later = an_obligation(conn, "OrgTruth: cut the release", n=2)
        assert [c["id"] for c in linking.candidates(conn)] == [later]


class TestItReachesThePlanner:
    def test_a_linked_commitment_can_enter_the_at_risk_tier(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The outcome that makes this feature real rather than a column being filled.
        Without a link the planner's whole at-risk band has nothing to promote."""
        from datetime import date

        from backglass.plan import planner

        goal = a_goal(conn, "Ship OrgTruth to first users")
        cid = an_obligation(conn, "OrgTruth: run e2e:live")

        day = date(2026, 8, 18)
        before = {
            c.commitment_id: c.priority
            for c in planner.candidates(conn, settings, day, at_risk_goals=set())
        }
        linking.apply(conn, [linking.Link(cid, goal, 0.9, "OrgTruth", None)])
        after = {
            c.commitment_id: c.priority
            for c in planner.candidates(conn, settings, day, at_risk_goals={goal})
        }

        assert before[cid] == planner.PRIORITY_REST
        assert after[cid] == planner.PRIORITY_AT_RISK_GOAL


class TestARowTheModelWillNotAnswerAbout:
    """Found by running the pass for real rather than by reasoning about it.

    The first live dry run sent 25 obligations and got 16 verdicts back. Nine were simply
    absent from the response — not refused, not discarded, just not mentioned. `candidates`
    skips only *judged* rows, so those nine would have been sent again on the next sync,
    and the one after, for ever: an unbounded cost loop with nothing anywhere reporting it,
    which is the shape of three other findings in this repo this week.
    """

    def _sent_and_ignored(self, conn: sqlite3.Connection, cid: int) -> None:
        from backglass import claim_events

        claim_events.record(
            conn, subject_table="commitment", subject_id=cid,
            cause=linking.CAUSE_UNANSWERED,
        )

    def test_one_silence_is_a_bad_batch_and_the_row_is_asked_again(
        self, conn: sqlite3.Connection
    ) -> None:
        a_goal(conn, "Ship OrgTruth to first users")
        cid = an_obligation(conn, "OrgTruth: run e2e:live")
        self._sent_and_ignored(conn, cid)

        assert [c["id"] for c in linking.candidates(conn)] == [cid]

    def test_a_second_silence_is_the_row_and_it_stops_being_asked(
        self, conn: sqlite3.Connection
    ) -> None:
        a_goal(conn, "Ship OrgTruth to first users")
        cid = an_obligation(conn, "OrgTruth: run e2e:live")
        for _ in range(linking.MAX_ATTEMPTS):
            self._sent_and_ignored(conn, cid)

        assert linking.candidates(conn) == []

    def test_giving_up_is_not_a_verdict(self, conn: sqlite3.Connection) -> None:
        """The distinction the cause name carries. Recording `goal:none` for a row the
        model never mentioned would be putting an answer in its mouth, and the row would
        then read as "judged, serves no goal" for ever."""
        a_goal(conn, "Ship OrgTruth to first users")
        cid = an_obligation(conn, "OrgTruth: run e2e:live")
        for _ in range(linking.MAX_ATTEMPTS):
            self._sent_and_ignored(conn, cid)

        row = conn.execute("SELECT goal_id FROM commitment WHERE id = ?", (cid,)).fetchone()
        assert row["goal_id"] is None
        judged = conn.execute(
            "SELECT COUNT(*) AS n FROM claim_event WHERE subject_id = ?"
            " AND cause IN (?, ?)",
            (cid, linking.CAUSE_LINKED, linking.CAUSE_NONE),
        ).fetchone()
        assert judged["n"] == 0

    def test_an_answer_after_a_silence_still_counts(
        self, conn: sqlite3.Connection
    ) -> None:
        """The attempt count gates sending, not writing. A row that gets skipped once and
        answered the next time is judged normally."""
        goal = a_goal(conn, "Ship OrgTruth to first users")
        cid = an_obligation(conn, "OrgTruth: run e2e:live")
        self._sent_and_ignored(conn, cid)

        linking.apply(conn, [linking.Link(cid, goal, 0.9, "OrgTruth", None)])
        row = conn.execute("SELECT goal_id FROM commitment WHERE id = ?", (cid,)).fetchone()
        assert row["goal_id"] == goal
        assert linking.candidates(conn) == []
