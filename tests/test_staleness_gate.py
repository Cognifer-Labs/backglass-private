"""A commitment the system is asking about is one it must stop scheduling.

Reported by the owner on 2026-08-18 looking at that afternoon's plan: "the AES things on
schedule make no sense because i go to asu". Four of twelve blocks were college-admissions
work whose deadlines lapsed on 2026-05-01 — a UT Dallas scholarship acceptance, a
UW–Madison waitlist form — for schools he does not attend, ranked above everything real
because `PRIORITY_OVERDUE` puts the most lapsed item first and nothing ever expires it.

The shape under test, in both halves:

  1. `staleness` is one predicate with two readers. The question surface asks about a row;
     the planner leaves the same row out until the owner answers.
  2. `STALE_KEEP` beats the rule. "Still on my plate" has to put the item back in the very
     next plan, or the gate quietly buries exactly the obligations the owner rescued.

And the second defect inside the same complaint: a block marked done left its commitment
open, so the board re-proposed it the same afternoon (block 518 `done`, commitment 69
`open`).
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from backglass import questions, staleness
from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID
from backglass.plan import planner
from backglass.web import actions

PHOENIX = "America/Phoenix"

#: A Tuesday, and the day the owner reported this.
TODAY = date(2026, 8, 18)


@pytest.fixture
def sett(settings: Settings) -> Settings:
    return settings.model_copy(update={"default_tz": PHOENIX, "confidence_threshold": 0.7})


def a_commitment(
    conn: sqlite3.Connection,
    what: str,
    *,
    due: str,
    occurred: str,
    minutes: int = 30,
    author: str = "AES@utdallas.edu",
) -> int:
    """One mail-shaped obligation, with the sighting that created it."""
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, content_hash, triage_verdict)"
        " VALUES (?, 'apple-mail', ?, ?, ?, ?, 'Reminder to accept your award', 'body',"
        " ?, 'keep')",
        (USER_ID, f"m-{what}", now_iso(), occurred, author, f"h-{what}"),
    )
    source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, estimated_minutes,"
        " estimate_source, confidence, status, source_item_id, created_at)"
        " VALUES (?, 'i_owe', ?, ?, ?, 'manual', 0.9, 'open', ?, ?)",
        (USER_ID, what, due, minutes, source_id, now_iso()),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


class TestThePlannerGate:
    def test_the_reported_day_no_longer_schedules_a_lapsed_admissions_deadline(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The exact row from the complaint: due 2026-05-01, last seen in April."""
        a_commitment(
            conn,
            "complete AES scholarship acceptance and agreement form",
            due="2026-05-01",
            occurred="2026-04-23T12:54:42-05:00",
        )
        a_commitment(
            conn,
            "email the McKenna cohort about Thursday",
            due="2026-08-19",
            occurred="2026-08-17T09:00:00-07:00",
            author="sheppard@asu.edu",
        )

        proposal = planner.propose(conn, sett, TODAY, events=[])

        titles = [str(b["title"]) for b in proposal.blocks]
        assert "email the McKenna cohort about Thursday" in titles
        assert not any("AES" in t for t in titles)
        # Not silently truncated either: P2's overflow is the "did not fit" list, and a
        # stale row did not fail to fit — it is not a candidate at all. So the plan says
        # so in its own notes instead, where the way back is the ask page.
        assert not any("AES" in c.what for c in proposal.overflow)
        assert any("1 long-lapsed item(s) held back" in n for n in proposal.notes)

    def test_the_gate_and_the_question_are_the_same_predicate(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Two readers, one rule. Drift between them is how the board ends up asking
        "still real?" about the same half hour it just scheduled."""
        cid = a_commitment(
            conn, "accept the award", due="2026-05-01", occurred="2026-04-23T10:00:00-07:00"
        )

        assert staleness.stale_ids(conn, TODAY) == {cid}
        asked = questions._stale_commitments(conn, sett, TODAY)
        assert [q.subject_key for q in asked] == [str(cid)]
        pool = planner.candidates(conn, sett, TODAY, set())
        assert cid not in {c.commitment_id for c in pool}

    def test_merely_overdue_still_outranks_everything(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """docs/04 §1.5 rule 2 is untouched inside the window. A deadline missed last
        week is the planner's whole job; the gate is only for decay."""
        recent = a_commitment(
            conn,
            "return the housing form",
            due="2026-08-14",
            occurred="2026-08-13T09:00:00-07:00",
            author="housing@asu.edu",
        )

        assert staleness.stale_ids(conn, TODAY) == set()
        cands = planner.candidates(conn, sett, TODAY, set())
        assert [c.commitment_id for c in cands] == [recent]
        assert cands[0].priority == planner.PRIORITY_OVERDUE

    def test_recent_evidence_keeps_a_long_overdue_item_in_the_plan(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Lapsed in May and mentioned yesterday is not decay — somebody still cares,
        and the planner must not hide it behind a question."""
        cid = a_commitment(
            conn,
            "pay the enrollment fee",
            due="2026-05-01",
            occurred="2026-04-20T10:00:00-07:00",
        )
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, title, body_text, content_hash, triage_verdict)"
            " VALUES (?, 'apple-mail', 'm-nudge', ?, '2026-08-17T09:00:00-07:00', 'Re:',"
            " 'still outstanding', 'h-nudge', 'keep')",
            (USER_ID, now_iso()),
        )
        nudge = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO commitment_evidence (user_id, commitment_id, source_item_id,"
            " kind, seen_at) VALUES (?, ?, ?, 'restated', ?)",
            (USER_ID, cid, nudge, now_iso()),
        )

        assert staleness.stale_ids(conn, TODAY) == set()
        assert cid in {c.commitment_id for c in planner.candidates(conn, sett, TODAY, set())}

    def test_keep_it_open_puts_it_back_in_the_next_plan(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The answer outranks the rule. Without this, "still on my plate" would mean
        "keep it open and never show it to me", which is the worst reading of all three
        buttons."""
        cid = a_commitment(
            conn,
            "finish the transfer-credit list",
            due="2026-05-01",
            occurred="2026-04-20T10:00:00-07:00",
        )
        questions.refresh(conn, sett, TODAY)
        qid = int(
            conn.execute(
                "SELECT id FROM open_question WHERE kind = 'stale' AND subject_key = ?",
                (str(cid),),
            ).fetchone()["id"]
        )

        questions.answer(conn, sett, qid, option=questions.STALE_KEEP)

        assert staleness.stale_ids(conn, TODAY) == set()
        titles = [
            str(b["title"]) for b in planner.propose(conn, sett, TODAY, events=[]).blocks
        ]
        assert "finish the transfer-credit list" in titles

    def test_dropping_it_is_not_the_same_as_keeping_it(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The KEEP override is matched on the exact option, so a dropped row does not
        come back through the same door it left by."""
        cid = a_commitment(
            conn, "accept the award", due="2026-05-01", occurred="2026-04-23T10:00:00-07:00"
        )
        questions.refresh(conn, sett, TODAY)
        qid = int(conn.execute("SELECT id FROM open_question").fetchone()["id"])

        questions.answer(conn, sett, qid, option=questions.STALE_DROP)

        assert conn.execute(
            "SELECT status FROM commitment WHERE id = ?", (cid,)
        ).fetchone()["status"] == "dropped"
        assert cid not in {
            c.commitment_id for c in planner.candidates(conn, sett, TODAY, set())
        }

    def test_the_gate_is_not_batched_like_the_questions_are(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The queue is paced by how many questions a person will answer in a day; the
        plan has to be right today. On the live ledger that difference is 104 lapsed rows
        against five questions a refresh — a batched gate would leave the board wrong for
        three weeks."""
        for i in range(staleness.STALE_BATCH_LIMIT + 3):
            a_commitment(
                conn, f"dead admissions task {i}", due=f"2026-05-{1 + i:02d}",
                occurred="2026-04-20T10:00:00-07:00",
            )

        asked = questions._stale_commitments(conn, sett, TODAY)
        assert len(asked) == staleness.STALE_BATCH_LIMIT
        assert len(staleness.stale_ids(conn, TODAY)) == staleness.STALE_BATCH_LIMIT + 3
        assert planner.candidates(conn, sett, TODAY, set()) == []


class TestBlockOutcomeReachesTheLedger:
    def test_done_on_the_board_closes_the_commitment(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Block 518 was `done` and commitment 69 was still `open`, so the planner
        proposed it again the same afternoon."""
        cid = a_commitment(
            conn,
            "accept the award",
            due="2026-08-18",
            occurred="2026-08-17T10:00:00-07:00",
        )
        block = _a_block(conn, cid)

        actions.set_block_outcome(conn, block, "done")

        row = conn.execute(
            "SELECT status, resolution_note FROM commitment WHERE id = ?", (cid,)
        ).fetchone()
        assert row["status"] == "done"
        assert str(row["resolution_note"]).startswith("marked done on the day plan")
        assert not planner.candidates(conn, sett, TODAY, set())

    def test_rolled_and_dropped_leave_the_promise_alone(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """`rolled` is counted on both rows by `rollover.close_day`, and a dropped block
        means "not this slot" — neither is the owner saying the promise is dead."""
        cid = a_commitment(
            conn, "accept the award", due="2026-08-18", occurred="2026-08-17T10:00:00-07:00"
        )
        for outcome in ("rolled", "dropped", "pending"):
            actions.set_block_outcome(conn, _a_block(conn, cid), outcome)
            assert conn.execute(
                "SELECT status FROM commitment WHERE id = ?", (cid,)
            ).fetchone()["status"] == "open"

    def test_a_block_with_no_commitment_behind_it_is_fine(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Routines and the small-items batch carry no commitment_id."""
        assert actions.set_block_outcome(conn, _a_block(conn, None), "done").ok

    def test_marking_done_twice_does_not_raise(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Another surface may have closed it first; the block's own outcome still stands."""
        cid = a_commitment(
            conn, "accept the award", due="2026-08-18", occurred="2026-08-17T10:00:00-07:00"
        )
        first, second = _a_block(conn, cid), _a_block(conn, cid)
        actions.set_block_outcome(conn, first, "done")

        assert actions.set_block_outcome(conn, second, "done").ok
        assert conn.execute(
            "SELECT outcome FROM plan_block WHERE id = ?", (second,)
        ).fetchone()["outcome"] == "done"

    def test_an_unknown_block_still_errors(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(actions.ActionError):
            actions.set_block_outcome(conn, 9999, "done")


def _a_block(conn: sqlite3.Connection, commitment_id: int | None) -> int:
    """One proposed plan block on the reported day."""
    row = conn.execute(
        "SELECT id FROM day_plan WHERE local_date = ?", (TODAY.isoformat(),)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO day_plan (user_id, local_date, tz, status, capacity_minutes,"
            " generated_at) VALUES (?, ?, ?, 'proposed', 480, ?)",
            (USER_ID, TODAY.isoformat(), PHOENIX, now_iso()),
        )
        plan_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    else:
        plan_id = int(row["id"])
    conn.execute(
        "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, commitment_id,"
        " title, user_id) VALUES (?, ?, ?, 'work', ?, 'accept the award', ?)",
        (
            plan_id,
            f"{TODAY.isoformat()}T15:00:00-07:00",
            f"{TODAY.isoformat()}T15:30:00-07:00",
            commitment_id,
            USER_ID,
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
