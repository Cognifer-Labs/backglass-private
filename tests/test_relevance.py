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


# ══ v3, 2026-08-24: what each obligation rests on ═════════════════════════


class TestDependenciesAreRecorded:
    """The gap this closes is `logic_check`'s own constraint: judged once per commitment,
    ever. A keep issued when the facts said one thing was never revisited when they said
    another — "the checker has no way to notice the situation moved", which is the owner's
    complaint restated in schema. Dependencies are the index that answers "which
    obligations did this fact hold up?", and nothing else in the ledger can.
    """

    def test_a_keep_records_what_it_rests_on(
        self, conn: sqlite3.Connection, sett: Settings, prompt
    ) -> None:  # type: ignore[no-untyped-def]
        from backglass import claim_events

        fact_id = a_fact(conn, "identity", "enrolment", "ASU Tempe")
        cid = an_obligation(conn, "submit the ASU housing form")
        work = relevance_mod.Work(
            facts=relevance_mod.facts_for(conn),
            commitments=relevance_mod.candidates(conn),
        )
        judged, _ = relevance_mod.parse(
            {"verdicts": [{
                "commitment_id": cid, "verdict": "keep", "confidence": 0.9,
                "depends_on": [fact_id], "reason": "live while enrolled at ASU",
            }]},
            work,
        )
        relevance_mod.apply(conn, sett, work, judged)

        deps = claim_events.active_dependencies(conn, "commitment", cid)
        assert [d.fact_id for d in deps] == [fact_id]
        assert deps[0].kind == "fact"

    def test_an_obligation_resting_on_nothing_records_that_too(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """`none` is first-class. An obligation judged to rest on no fact is a different
        thing from one nobody has judged — `claim_events` requires an empty result to read
        as *unknown*, and writing this row is what moves it to *confirmed independent*."""
        from backglass import claim_events

        a_fact(conn, "identity", "enrolment", "ASU Tempe")
        cid = an_obligation(conn, "bring the bedsheet to wash")
        work = relevance_mod.Work(
            facts=relevance_mod.facts_for(conn),
            commitments=relevance_mod.candidates(conn),
        )
        judged, _ = relevance_mod.parse(
            {"verdicts": [{
                "commitment_id": cid, "verdict": "keep", "confidence": 0.8,
                "depends_on": [], "reason": "household, rests on nothing recorded",
            }]},
            work,
        )
        relevance_mod.apply(conn, sett, work, judged)

        deps = claim_events.active_dependencies(conn, "commitment", cid)
        assert [d.kind for d in deps] == ["none"]

    def test_an_id_the_pass_never_sent_is_dropped_from_the_dependencies(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The 2026-08-12 guard, applied to the new field: the tables overlap in range, so
        an invented id would otherwise become a dependency pointing at an unrelated row —
        and a wrong dependency is what later decides an obligation is dead.

        Filtered rather than voiding the verdict, unlike a bad `cites_fact`: a citation
        justifies closing something and must be right or absent; a dependency is a note
        about what to re-check, and a narrower index is not a wrong answer.
        """
        from backglass import claim_events

        real = a_fact(conn, "identity", "enrolment", "ASU Tempe")
        cid = an_obligation(conn, "submit the ASU housing form")
        work = relevance_mod.Work(
            facts=relevance_mod.facts_for(conn),
            commitments=relevance_mod.candidates(conn),
        )
        judged, discarded = relevance_mod.parse(
            {"verdicts": [{
                "commitment_id": cid, "verdict": "keep", "confidence": 0.9,
                "depends_on": [real, 99999], "reason": "keep",
            }]},
            work,
        )
        assert discarded == 0, "the verdict itself still stands"
        relevance_mod.apply(conn, sett, work, judged)

        deps = claim_events.active_dependencies(conn, "commitment", cid)
        assert [d.fact_id for d in deps] == [real]


class TestABrokenDependencyReopensTheVerdict:
    def _judged_keep(self, conn: sqlite3.Connection, sett: Settings) -> tuple[int, int]:
        fact_id = a_fact(conn, "identity", "enrolment", "UT Dallas")
        cid = an_obligation(conn, "accept the UT Dallas award")
        work = relevance_mod.Work(
            facts=relevance_mod.facts_for(conn),
            commitments=relevance_mod.candidates(conn),
        )
        judged, _ = relevance_mod.parse(
            {"verdicts": [{
                "commitment_id": cid, "verdict": "keep", "confidence": 0.9,
                "depends_on": [fact_id], "reason": "live while enrolled there",
            }]},
            work,
        )
        relevance_mod.apply(conn, sett, work, judged)
        return fact_id, cid

    def test_a_verdict_from_before_v3_is_sent_once_to_record_what_it_rests_on(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Otherwise the index is live and permanently empty. Every open obligation on the
        owner's ledger was judged in one pass on 2026-08-19, before the question existed;
        none would ever be re-queued, so none would ever record a dependency, so nothing
        could ever be invalidated."""
        a_fact(conn, "identity", "enrolment", "ASU Tempe")
        cid = an_obligation(conn, "an obligation judged before v3")
        conn.execute(
            "INSERT INTO logic_check (user_id, commitment_id, verdict, confidence,"
            " status, created_at, decided_at) VALUES (?, ?, 'keep', 0.9, 'kept', ?, ?)",
            (USER_ID, cid, "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z"),
        )

        assert cid in {c["id"] for c in relevance_mod.candidates(conn)}

    def test_and_it_is_sent_exactly_once(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The backfill has to terminate. A v3 judgment always writes a dependency row —
        a fact, or `none` — so the clause is false for that commitment forever after."""
        a_fact(conn, "identity", "enrolment", "ASU Tempe")
        cid = an_obligation(conn, "an obligation judged before v3")
        conn.execute(
            "INSERT INTO logic_check (user_id, commitment_id, verdict, confidence,"
            " status, created_at, decided_at) VALUES (?, ?, 'keep', 0.9, 'kept', ?, ?)",
            (USER_ID, cid, "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z"),
        )
        work = relevance_mod.Work(
            facts=relevance_mod.facts_for(conn),
            commitments=relevance_mod.candidates(conn),
        )
        judged, _ = relevance_mod.parse(
            {"verdicts": [{
                "commitment_id": cid, "verdict": "keep", "confidence": 0.9,
                "depends_on": [], "reason": "rests on nothing recorded",
            }]},
            work,
        )
        relevance_mod.apply(conn, sett, work, judged)

        assert cid not in {c["id"] for c in relevance_mod.candidates(conn)}

    def test_an_unchanged_ledger_re_judges_nothing(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Rule 3, and the expensive failure mode: a re-queue predicate that stays true
        would re-judge and re-pay for the same obligation every sync forever."""
        _, cid = self._judged_keep(conn, sett)

        assert cid not in {c["id"] for c in relevance_mod.candidates(conn)}

    def test_superseding_the_fact_puts_it_back_in_the_queue(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The whole point. `facts.remember` breaks the dependency and writes the event;
        this is the pass noticing."""
        from backglass import facts

        _, cid = self._judged_keep(conn, sett)
        facts.remember(conn, sett, "identity", "enrolment", "ASU Tempe")

        assert cid in {c["id"] for c in relevance_mod.candidates(conn)}

    def test_a_re_judgment_replaces_the_verdict_and_leaves_the_queue(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """And it must actually settle: a re-judgment that did not overwrite the old row
        would leave its old `decided_at` in place and re-queue on the very next sync."""
        from backglass import facts

        _, cid = self._judged_keep(conn, sett)
        facts.remember(conn, sett, "identity", "enrolment", "ASU Tempe")

        work = relevance_mod.Work(
            facts=relevance_mod.facts_for(conn),
            commitments=relevance_mod.candidates(conn),
        )
        judged, _ = relevance_mod.parse(
            {"verdicts": [{
                "commitment_id": cid, "verdict": "keep", "confidence": 0.7,
                "depends_on": [], "reason": "still theirs to do",
            }]},
            work,
        )
        relevance_mod.apply(conn, sett, work, judged)

        assert cid not in {c["id"] for c in relevance_mod.candidates(conn)}
        rows = conn.execute(
            "SELECT verdict, confidence, reason FROM logic_check WHERE commitment_id = ?",
            (cid,),
        ).fetchall()
        assert len(rows) == 1, "the verdict is replaced, not duplicated"
        # And it is the NEW verdict. `DO NOTHING` would leave the old row sitting there
        # looking settled while saying something the pass no longer believes.
        assert rows[0]["confidence"] == 0.7
        assert rows[0]["reason"] == "still theirs to do"
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM claim_event WHERE cause = 'relevance_rejudged'"
        ).fetchone()["n"] == 1, "and the change is in the ledger that records changes"

    def test_a_question_waiting_on_the_owner_is_never_re_opened(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """A `pending` verdict is a question already in front of the owner. Asking the
        model again spends money on a decision waiting on a person, and risks
        contradicting the card they are looking at."""
        from backglass import facts

        fact_id = a_fact(conn, "identity", "enrolment", "UT Dallas")
        cid = an_obligation(conn, "accept the UT Dallas award")
        work = relevance_mod.Work(
            facts=relevance_mod.facts_for(conn),
            commitments=relevance_mod.candidates(conn),
        )
        judged, _ = relevance_mod.parse(
            {"verdicts": [{
                "commitment_id": cid, "verdict": "nonsense", "confidence": 0.5,
                "cites_fact": fact_id, "quote": "Academic Excellence Scholarship",
                "depends_on": [fact_id], "reason": "looks overtaken",
            }]},
            work,
        )
        relevance_mod.apply(conn, sett, work, judged)
        assert conn.execute(
            "SELECT status FROM logic_check WHERE commitment_id = ?", (cid,)
        ).fetchone()["status"] == "pending", "precondition: it asked rather than dropped"

        facts.remember(conn, sett, "identity", "enrolment", "ASU Tempe")

        assert cid not in {c["id"] for c in relevance_mod.candidates(conn)}
