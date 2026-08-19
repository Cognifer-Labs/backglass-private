"""Obligations a recorded fact has overtaken, retired against the fact that did it.

The owner enrolled at ASU and the board kept scheduling a UT Dallas scholarship
acceptance. `fact` row 5 said so all along and nothing read it.

Same shape as tests/test_recheck.py, for the same reason: the negatives are the file. A
kept row costs a click; a dropped one disappears, and four entries in tasks/lessons.md are
about paying for that. So every guard in `relevance.parse` gets a test that fails without
it, and the confidence asymmetry gets one too.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from backglass.config import Settings
from backglass.extract import prompts
from backglass.extract import relevance as relevance_mod
from backglass.ledger import USER_ID

UTD = (
    "Congratulations on your admission and the offer of an Academic Excellence "
    "Scholarship to The University of Texas at Dallas! To receive your award, you need "
    "to review the AES Academic Agreement Form by May 1, 2026."
)


@pytest.fixture
def sett(settings: Settings) -> Settings:
    return settings.model_copy(update={"relevance_drop_confidence": 0.85})


@pytest.fixture
def prompt():  # type: ignore[no-untyped-def]
    return prompts.load("check-relevance")


def a_fact(conn: sqlite3.Connection, subject: str, key: str, value: str) -> int:
    conn.execute(
        "INSERT INTO fact (user_id, subject, key, value, source, status, created_at)"
        " VALUES (?, ?, ?, ?, 'manual', 'active', '2026-08-01T00:00:00Z')",
        (USER_ID, subject, key, value),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def an_obligation(
    conn: sqlite3.Connection,
    what: str,
    *,
    body: str = UTD,
    author: str = "AES@utdallas.edu",
    due: str | None = "2026-05-01",
) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, content_hash, triage_verdict)"
        " VALUES (?, 'apple-mail', ?, '2026-08-01T00:00:00Z', '2026-04-23T12:54:42-05:00',"
        " ?, 'Reminder to Accept your Award', ?, ?, 'keep')",
        (USER_ID, f"m-{what}", author, body, f"h-{what}"),
    )
    item = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, confidence, status,"
        " estimated_minutes, estimate_source, source_item_id, created_at)"
        " VALUES (?, 'i_owe', ?, ?, 0.9, 'open', 30, 'manual', ?, '2026-08-01T00:00:00Z')",
        (USER_ID, what, due, item),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


class FakeResult:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.cost_usd = 0.02


class FakeClient:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls = 0

    def complete(self, **_kwargs: Any) -> FakeResult:
        self.calls += 1
        return FakeResult(self.data)


def verdict(
    commitment_id: int,
    *,
    kind: str = "nonsense",
    confidence: float = 0.95,
    fact: int | None = None,
    quote: str | None = "The University of Texas at Dallas",
) -> dict[str, Any]:
    return {
        "commitment_id": commitment_id,
        "verdict": kind,
        "confidence": confidence,
        "cites_fact": fact,
        "quote": quote,
        "reason": "the owner enrolled elsewhere",
    }


def status_of(conn: sqlite3.Connection, commitment_id: int) -> str:
    return str(
        conn.execute(
            "SELECT status FROM commitment WHERE id = ?", (commitment_id,)
        ).fetchone()["status"]
    )


class TestTheReportedCase:
    def test_the_other_universitys_deadline_is_dropped_citing_the_enrolment(
        self, conn: sqlite3.Connection, sett: Settings, prompt: Any
    ) -> None:
        fact = a_fact(conn, "education", "college", "ASU Tempe, Barrett Honors, fall 2026")
        cid = an_obligation(conn, "complete AES scholarship acceptance")
        client = FakeClient({"verdicts": [verdict(cid, fact=fact)]})

        report = relevance_mod.run(conn, sett, client, prompt=prompt)

        assert report.dropped == 1
        row = conn.execute(
            "SELECT status, resolution_note FROM commitment WHERE id = ?", (cid,)
        ).fetchone()
        assert row["status"] == "dropped"
        assert f"fact {fact}" in str(row["resolution_note"])
        assert "ASU Tempe" in str(row["resolution_note"])

    def test_a_ledger_with_no_facts_makes_no_call_at_all(
        self, conn: sqlite3.Connection, sett: Settings, prompt: Any
    ) -> None:
        """With nothing recorded to contradict, this pass has no business guessing from
        the obligations alone — and no business spending money to do it."""
        an_obligation(conn, "complete AES scholarship acceptance")
        client = FakeClient({"verdicts": []})

        relevance_mod.run(conn, sett, client, prompt=prompt)

        assert client.calls == 0


class TestTheGuards:
    def test_an_uncited_nonsense_verdict_is_discarded(
        self, conn: sqlite3.Connection, sett: Settings, prompt: Any
    ) -> None:
        """"This looks irrelevant" is the reasoning that would delete the obligations the
        owner is quietly failing. No fact id, no drop."""
        a_fact(conn, "education", "college", "ASU Tempe")
        cid = an_obligation(conn, "complete AES scholarship acceptance")
        client = FakeClient({"verdicts": [verdict(cid, fact=None)]})

        report = relevance_mod.run(conn, sett, client, prompt=prompt)

        assert report.discarded == 1 and report.dropped == 0
        assert status_of(conn, cid) == "open"

    def test_a_fact_id_that_was_never_sent_is_discarded(
        self, conn: sqlite3.Connection, sett: Settings, prompt: Any
    ) -> None:
        """Every id in this codebase is a bare int and the tables overlap in range
        (2026-08-12): an invented fact id must not read as a citation."""
        a_fact(conn, "education", "college", "ASU Tempe")
        cid = an_obligation(conn, "complete AES scholarship acceptance")
        client = FakeClient({"verdicts": [verdict(cid, fact=9999)]})

        report = relevance_mod.run(conn, sett, client, prompt=prompt)

        assert report.discarded == 1
        assert status_of(conn, cid) == "open"

    def test_a_quote_that_is_not_in_the_obligation_is_discarded(
        self, conn: sqlite3.Connection, sett: Settings, prompt: Any
    ) -> None:
        """The guard against a fluent paraphrase standing in for having read the thing."""
        fact = a_fact(conn, "education", "college", "ASU Tempe")
        cid = an_obligation(conn, "complete AES scholarship acceptance")
        client = FakeClient(
            {"verdicts": [verdict(cid, fact=fact, quote="Baylor University")]}
        )

        report = relevance_mod.run(conn, sett, client, prompt=prompt)

        assert report.discarded == 1
        assert status_of(conn, cid) == "open"

    def test_a_commitment_id_that_was_never_sent_is_discarded(
        self, conn: sqlite3.Connection, sett: Settings, prompt: Any
    ) -> None:
        fact = a_fact(conn, "education", "college", "ASU Tempe")
        an_obligation(conn, "complete AES scholarship acceptance")
        client = FakeClient({"verdicts": [verdict(4242, fact=fact)]})

        report = relevance_mod.run(conn, sett, client, prompt=prompt)

        assert report.discarded == 1 and report.judged == 0

    def test_a_commitment_closed_since_the_call_is_left_alone(
        self, conn: sqlite3.Connection, sett: Settings, prompt: Any
    ) -> None:
        from backglass.web import actions

        fact = a_fact(conn, "education", "college", "ASU Tempe")
        cid = an_obligation(conn, "complete AES scholarship acceptance")
        work = relevance_mod.Work(
            facts=relevance_mod.facts_for(conn),
            commitments=relevance_mod.candidates(conn),
        )
        judgements, _ = relevance_mod.parse(
            {"verdicts": [verdict(cid, fact=fact)]}, work
        )
        actions.resolve(conn, cid, note="board click")

        report = relevance_mod.apply(conn, sett, work, judgements)

        assert report.judged == 0 and report.dropped == 0
        assert status_of(conn, cid) == "done"


class TestTheAsymmetry:
    def test_below_the_threshold_it_asks_instead_of_dropping(
        self, conn: sqlite3.Connection, sett: Settings, prompt: Any
    ) -> None:
        fact = a_fact(conn, "education", "college", "ASU Tempe")
        cid = an_obligation(conn, "complete AES scholarship acceptance")
        client = FakeClient({"verdicts": [verdict(cid, fact=fact, confidence=0.6)]})

        report = relevance_mod.run(conn, sett, client, prompt=prompt)

        assert report.asked == 1 and report.dropped == 0
        assert status_of(conn, cid) == "open"
        row = conn.execute(
            "SELECT kind, subject_key, options_json FROM open_question"
        ).fetchone()
        assert row["kind"] == "nonsense" and row["subject_key"] == str(cid)
        assert relevance_mod.RELEVANCE_DROP in str(row["options_json"])

    def test_the_owners_click_is_what_drops_it(
        self, conn: sqlite3.Connection, sett: Settings, prompt: Any
    ) -> None:
        from backglass import questions

        fact = a_fact(conn, "education", "college", "ASU Tempe")
        cid = an_obligation(conn, "complete AES scholarship acceptance")
        relevance_mod.run(
            conn,
            sett,
            FakeClient({"verdicts": [verdict(cid, fact=fact, confidence=0.6)]}),
            prompt=prompt,
        )
        qid = int(conn.execute("SELECT id FROM open_question").fetchone()["id"])

        questions.answer(conn, sett, qid, option=relevance_mod.RELEVANCE_DROP)

        assert status_of(conn, cid) == "dropped"
        assert conn.execute(
            "SELECT status FROM logic_check WHERE commitment_id = ?", (cid,)
        ).fetchone()["status"] == "applied"

    def test_keeping_it_settles_the_verdict_rather_than_snoozing_it(
        self, conn: sqlite3.Connection, sett: Settings, prompt: Any
    ) -> None:
        """The judge already suspected this row; asking again next week would be asking a
        question the owner answered."""
        from backglass import questions

        fact = a_fact(conn, "education", "college", "ASU Tempe")
        cid = an_obligation(conn, "complete AES scholarship acceptance")
        relevance_mod.run(
            conn,
            sett,
            FakeClient({"verdicts": [verdict(cid, fact=fact, confidence=0.6)]}),
            prompt=prompt,
        )
        qid = int(conn.execute("SELECT id FROM open_question").fetchone()["id"])

        questions.answer(conn, sett, qid, option=relevance_mod.RELEVANCE_KEEP)

        assert status_of(conn, cid) == "open"
        assert conn.execute(
            "SELECT status FROM logic_check WHERE commitment_id = ?", (cid,)
        ).fetchone()["status"] == "kept"
        assert relevance_mod.candidates(conn) == []


    def test_waving_the_question_away_settles_it_rather_than_stranding_the_row(
        self, conn: sqlite3.Connection, sett: Settings, prompt: Any
    ) -> None:
        """Dismissal is normally "nothing is recorded as settled". Here something is
        waiting on the answer: the judge reads judged-once, so a dismissed question with
        a `pending` verdict would leave the obligation open, unasked-about and unjudgeable
        forever. Waving this one away means leave my row alone."""
        from backglass import questions

        fact = a_fact(conn, "education", "college", "ASU Tempe")
        cid = an_obligation(conn, "complete AES scholarship acceptance")
        relevance_mod.run(
            conn,
            sett,
            FakeClient({"verdicts": [verdict(cid, fact=fact, confidence=0.6)]}),
            prompt=prompt,
        )
        qid = int(conn.execute("SELECT id FROM open_question").fetchone()["id"])

        questions.dismiss(conn, qid)

        assert status_of(conn, cid) == "open"
        assert conn.execute(
            "SELECT status FROM logic_check WHERE commitment_id = ?", (cid,)
        ).fetchone()["status"] == "kept"


class TestJudgedOnce:
    def test_a_second_pass_over_an_unchanged_ledger_makes_no_call(
        self, conn: sqlite3.Connection, sett: Settings, prompt: Any
    ) -> None:
        """Rule 3 on a 30-minute timer: judged-once is what stops two hundred obligations
        being re-decided, and re-paid for, every sync."""
        fact = a_fact(conn, "education", "college", "ASU Tempe")
        cid = an_obligation(conn, "submit the housing form", author="housing@asu.edu")
        client = FakeClient({"verdicts": [verdict(cid, kind="keep", fact=fact)]})
        relevance_mod.run(conn, sett, client, prompt=prompt)
        assert client.calls == 1

        relevance_mod.run(conn, sett, client, prompt=prompt)

        assert client.calls == 1
        assert status_of(conn, cid) == "open"

    def test_a_dry_run_writes_nothing_and_stays_judgeable(
        self, conn: sqlite3.Connection, sett: Settings, prompt: Any
    ) -> None:
        fact = a_fact(conn, "education", "college", "ASU Tempe")
        cid = an_obligation(conn, "complete AES scholarship acceptance")
        client = FakeClient({"verdicts": [verdict(cid, fact=fact)]})

        report = relevance_mod.run(conn, sett, client, prompt=prompt, dry_run=True)

        assert report.dropped == 1
        assert status_of(conn, cid) == "open"
        assert conn.execute("SELECT COUNT(*) AS n FROM logic_check").fetchone()["n"] == 0
        assert [c["id"] for c in relevance_mod.candidates(conn)] == [cid]

    def test_the_batch_is_split_rather_than_sent_as_one_call(
        self, conn: sqlite3.Connection, sett: Settings, prompt: Any
    ) -> None:
        a_fact(conn, "education", "college", "ASU Tempe")
        for i in range(relevance_mod.BATCH + 2):
            an_obligation(conn, f"obligation {i}")
        client = FakeClient({"verdicts": []})

        relevance_mod.run(conn, sett, client, prompt=prompt)

        assert client.calls == 2


class TestThePromptCarriesWhatTheGuardsCheck:
    def test_both_halves_are_rendered_with_ids_the_verdict_can_cite(
        self, conn: sqlite3.Connection, prompt: Any
    ) -> None:
        """Every guard downstream is an id comparison, so the ids have to be in the
        prompt — a rendering that drops them makes every verdict undiscardable garbage."""
        fact = a_fact(conn, "education", "college", "ASU Tempe, Barrett Honors")
        cid = an_obligation(conn, "complete AES scholarship acceptance")
        work = relevance_mod.Work(
            facts=relevance_mod.facts_for(conn),
            commitments=relevance_mod.candidates(conn),
        )

        static, user = relevance_mod.render(work, prompt=prompt)

        assert f"[fact {fact}]" in user and "ASU Tempe, Barrett Honors" in user
        assert f"[{cid}]" in user and "AES@utdallas.edu" in user
        assert "University of Texas at Dallas" in user  # the source text, for the quote
        assert "cites_fact" in static  # the rule that makes the citation mandatory
