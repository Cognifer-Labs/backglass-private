"""Confusions Backglass cannot settle, asked instead of guessed.

The review queue asks about one record's confidence. These are the questions that do not
fit that shape: two classes at the same hour are each perfectly confident and cannot both
be attended; an untitled hour is not a doubtful commitment; two entities may be one
person. Each silently degrades the plan while nothing anywhere says so.

The invariants under test are the ones that decide whether a question surface gets used
twice: it asks once, it never guesses in the meantime, and the owner's own words are a
complete answer rather than a fallback.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backglass import decisions, questions
from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID
from backglass.plan.capacity import FixedEvent

MONDAY = date(2026, 8, 24)
PHOENIX = ZoneInfo("America/Phoenix")


def at(day: date, hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=PHOENIX)


def a_class(conn: sqlite3.Connection, day: date, start: str, end: str, title: str) -> None:
    """A fixed calendar event, through the path `capacity.day_events` actually reads."""
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash, triage_verdict)"
        " VALUES (?, 'calendar:apple', ?, ?, ?, NULL, ?, '', '{}', ?, 'keep')",
        (USER_ID, f"{title}-{day}-{start}", now_iso(), f"{day}T{start}:00-07:00", title,
         f"h-{title}-{day}-{start}"),
    )
    source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO engagement (user_id, kind, what, starts_at, ends_at, when_is_explicit,"
        " location, status, confidence, source_item_id, created_at)"
        " VALUES (?, 'professional', ?, ?, ?, 1, NULL, 'confirmed', 0.95, ?, ?)",
        (USER_ID, title, f"{day}T{start}:00-07:00", f"{day}T{end}:00-07:00", source_id,
         now_iso()),
    )


@pytest.fixture
def sett(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"working_window": "08:00-22:00", "working_days": ["mon", "tue", "wed",
                                                                  "thu", "fri", "sat", "sun"]}
    )


class TestItAsksAboutWhatItCannotSettle:
    def test_two_things_at_one_hour_become_a_question(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The owner's real 2026-08-24: LIA 101 10:10–11:00 against BIO 181 10:30–11:45.
        Stale timetable, dropped section, or a registration clash — nothing in the ledger
        can tell, and the difference matters before term starts."""
        a_class(conn, MONDAY, "10:10", "11:00", "LIA 101")
        a_class(conn, MONDAY, "10:30", "11:45", "BIO 181")

        found = questions.detect(conn, sett, MONDAY)

        conflicts = [q for q in found if q.kind == "conflict"]
        assert len(conflicts) == 1
        assert "LIA 101" in conflicts[0].detail and "BIO 181" in conflicts[0].detail
        assert "Attending LIA 101" in conflicts[0].options
        assert "Neither is right" in conflicts[0].options

    def test_back_to_back_classes_are_not_a_conflict(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        a_class(conn, MONDAY, "09:00", "10:00", "CIS 236")
        a_class(conn, MONDAY, "10:00", "11:00", "BIO 181")
        assert [q for q in questions.detect(conn, sett, MONDAY) if q.kind == "conflict"] == []

    def test_a_timetable_clash_is_one_question_however_many_weeks_it_repeats(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Asking about the same two classes on the 24th, the 31st and the 7th is asking
        one question three times, and a surface that does that is one nobody opens twice."""
        for week in range(3):
            day = MONDAY + timedelta(days=7 * week)
            a_class(conn, day, "10:10", "11:00", "LIA 101")
            a_class(conn, day, "10:30", "11:45", "BIO 181")

        conflicts = [q for q in questions.detect(conn, sett, MONDAY) if q.kind == "conflict"]
        assert len(conflicts) == 1
        assert "3 days" in conflicts[0].question

    def test_an_hour_with_no_name_is_asked_about_not_silently_spent(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        a_class(conn, MONDAY, "09:00", "10:00", "New Event")
        untitled = [q for q in questions.detect(conn, sett, MONDAY) if q.kind == "untitled"]
        assert len(untitled) == 1
        assert "60 minutes" in untitled[0].question

    def test_a_placeholder_never_becomes_a_conflict_question(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """"Is CIS 236 or New Event real?" invites an answer about the wrong thing. The
        owner cannot choose between a class and a name Calendar.app invented, and the
        placeholder already has its own, better question."""
        a_class(conn, MONDAY, "09:00", "10:15", "CIS 236")
        a_class(conn, MONDAY, "09:00", "10:00", "New Event")

        found = questions.detect(conn, sett, MONDAY)
        assert [q for q in found if q.kind == "conflict"] == []
        assert len([q for q in found if q.kind == "untitled"]) == 1

    def test_one_detector_failing_does_not_take_the_surface_down(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """Rule 5, applied to questions: a morning with no questions because something
        threw is indistinguishable from a morning with nothing to ask."""
        a_class(conn, MONDAY, "09:00", "10:00", "New Event")

        def boom(*args: object, **kwargs: object) -> list[questions.Question]:
            raise RuntimeError("detector down")

        monkeypatch.setattr(questions, "_conflicts", boom)
        assert [q.kind for q in questions.detect(conn, sett, MONDAY)] == ["untitled"]


class TestItAsksOnce:
    def test_refreshing_twice_does_not_ask_again(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        a_class(conn, MONDAY, "10:10", "11:00", "LIA 101")
        a_class(conn, MONDAY, "10:30", "11:45", "BIO 181")

        first = questions.refresh(conn, sett, MONDAY)
        second = questions.refresh(conn, sett, MONDAY)

        assert first >= 1
        assert second == 0
        assert len(questions.open_questions(conn)) == first

    def test_an_answered_question_never_returns(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        a_class(conn, MONDAY, "10:10", "11:00", "LIA 101")
        a_class(conn, MONDAY, "10:30", "11:45", "BIO 181")
        questions.refresh(conn, sett, MONDAY)
        asked = questions.open_questions(conn)[0]

        questions.answer(conn, sett, int(asked["id"]), option="Attending LIA 101")
        questions.refresh(conn, sett, MONDAY)

        assert all(q["id"] != asked["id"] for q in questions.open_questions(conn))


class TestTheOwnersOwnWordsAreACompleteAnswer:
    def _one(self, conn: sqlite3.Connection, sett: Settings) -> int:
        a_class(conn, MONDAY, "10:10", "11:00", "LIA 101")
        a_class(conn, MONDAY, "10:30", "11:45", "BIO 181")
        questions.refresh(conn, sett, MONDAY)
        return int(questions.open_questions(conn)[0]["id"])

    def test_free_text_alone_answers_it(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The options are the cases a detector thought of. The fifth case the owner knows
        about is usually the true one, so the box is never secondary to the buttons."""
        qid = self._one(conn, sett)
        questions.answer(conn, sett, qid, text="I swapped into the Tuesday section last week")

        answered = conn.execute(
            "SELECT status, answer_text, answer_option FROM open_question WHERE id = ?", (qid,)
        ).fetchone()
        assert answered["status"] == "answered"
        assert "Tuesday section" in answered["answer_text"]
        assert answered["answer_option"] is None

    def test_an_answer_outlives_the_question_as_a_decision(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """`decisions` already carries supersession and provenance in words, so the answer
        is recorded where the owner's settled choices live rather than only here."""
        qid = self._one(conn, sett)
        questions.answer(conn, sett, qid, text="Dropped LIA 101")

        titles = {d.choice for d in decisions.active(conn)}
        assert "Dropped LIA 101" in titles

    def test_an_empty_answer_is_refused(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        qid = self._one(conn, sett)
        with pytest.raises(ValueError):
            questions.answer(conn, sett, qid)

    def test_dismissing_settles_nothing_and_still_stops_asking(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Distinct from answered on purpose: waving a question away is not a ruling, and
        recording it as one would put words in the owner's mouth."""
        qid = self._one(conn, sett)
        questions.dismiss(conn, qid)

        assert questions.open_questions(conn) == []
        assert decisions.active(conn) == []


class TestTheAskPage:
    """One question, the whole screen, and a box that is not labelled "other"."""

    @pytest.fixture
    def client(self, conn: sqlite3.Connection, sett: Settings):  # type: ignore[no-untyped-def]
        from fastapi.testclient import TestClient

        from backglass.web.app import create_app

        del conn
        return TestClient(create_app(sett), base_url="http://127.0.0.1:8765")

    def _a_conflict(self, conn: sqlite3.Connection) -> None:
        a_class(conn, MONDAY, "10:10", "11:00", "LIA 101")
        a_class(conn, MONDAY, "10:30", "11:45", "BIO 181")

    def test_it_shows_one_question_and_nothing_to_navigate_away_to(
        self, client, conn: sqlite3.Connection
    ) -> None:  # type: ignore[no-untyped-def]
        """No shell, no sidebar. A question competing with nine navigation targets is a
        question that loses, which is how the review queue reached 303 open rows."""
        self._a_conflict(conn)
        body = client.get("/ask").text

        assert "Two things at once" in body
        assert "Attending LIA 101" in body
        assert 'class="side"' not in body  # the sidebar is deliberately absent
        assert body.count("askq") == 1  # exactly one question on screen

    def test_the_owners_own_words_are_offered_as_an_equal(
        self, client, conn: sqlite3.Connection
    ) -> None:  # type: ignore[no-untyped-def]
        self._a_conflict(conn)
        body = client.get("/ask").text

        assert 'name="text"' in body
        assert "answer in your own words" in body
        # Never "other", which would rank the owner's answer below the guesses.
        assert ">other<" not in body.lower()

    def test_free_text_is_saved_and_becomes_a_decision(
        self, client, conn: sqlite3.Connection
    ) -> None:  # type: ignore[no-untyped-def]
        self._a_conflict(conn)
        client.get("/ask")
        qid = int(conn.execute("SELECT id FROM open_question LIMIT 1").fetchone()["id"])

        client.post(
            f"/ask/{qid}/answer",
            data={"text": "LIA 101 is online, no clash"},
            follow_redirects=False,
        )

        assert decisions.active(conn)[0].choice == "LIA 101 is online, no clash"
        assert questions.open_questions(conn) == []

    def test_words_beat_a_button_when_both_arrive(
        self, client, conn: sqlite3.Connection
    ) -> None:  # type: ignore[no-untyped-def]
        """Pressing an option and also typing means the owner had more to say than the
        button carried. Keeping the button would throw away the part they bothered with."""
        self._a_conflict(conn)
        client.get("/ask")
        qid = int(conn.execute("SELECT id FROM open_question LIMIT 1").fetchone()["id"])

        client.post(
            f"/ask/{qid}/answer",
            data={"option": "Attending LIA 101", "text": "actually I dropped both"},
            follow_redirects=False,
        )

        assert decisions.active(conn)[0].choice == "actually I dropped both"

    def test_with_nothing_to_ask_it_says_so_rather_than_inventing_one(
        self, client
    ) -> None:  # type: ignore[no-untyped-def]
        body = client.get("/ask").text
        assert "no questions for you" in body
        assert "askopt" not in body

    def test_the_dashboard_offers_the_way_in(
        self, client, conn: sqlite3.Connection
    ) -> None:  # type: ignore[no-untyped-def]
        """Unreachable is the same as absent. The alert is the only route to the page."""
        self._a_conflict(conn)
        client.get("/ask")  # detection runs on arrival
        body = client.get("/").text

        assert 'href="/ask"' in body
        assert "only you can answer" in body


def _open_commitment(
    conn: sqlite3.Connection,
    what: str,
    *,
    due: str,
    occurred: str,
) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, content_hash, triage_verdict)"
        " VALUES (?, 'apple-mail', ?, ?, ?, 'Subject', 'body', ?, 'keep')",
        (USER_ID, f"m-{what}", occurred, occurred, f"h-{what}"),
    )
    sid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, confidence, status,"
        " source_item_id, created_at) VALUES (?, 'i_owe', ?, ?, 0.9, 'open', ?, ?)",
        (USER_ID, what, due, sid, occurred),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


class TestStaleCommitments:
    """Overdue plus silence is a QUESTION, never a close — in mail, silence is even
    weaker evidence than in chat, and a wrong close is silent data loss."""

    def test_long_overdue_and_unmentioned_raises_a_question(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        cid = _open_commitment(
            conn, "send the transcript", due="2026-07-01", occurred="2026-06-20T10:00:00Z"
        )
        found = questions._stale_commitments(conn, sett, MONDAY)
        assert len(found) == 1
        q = found[0]
        assert q.kind == "stale" and q.subject_key == str(cid)
        assert "send the transcript" in q.question
        assert "apple-mail · 2026-06-20" in q.detail
        # And nothing changed on the board: detection is read-only.
        status = conn.execute("SELECT status FROM commitment WHERE id = ?", (cid,)).fetchone()
        assert status["status"] == "open"

    def test_recent_evidence_resets_the_silence_clock(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """A restatement two days ago means somebody still cares, however overdue —
        the newest sighting is judged, not the original message."""
        cid = _open_commitment(
            conn, "send the transcript", due="2026-07-01", occurred="2026-06-20T10:00:00Z"
        )
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, title, body_text, content_hash, triage_verdict)"
            " VALUES (?, 'apple-mail', 'm-nudge', ?, '2026-08-22T09:00:00Z', 'Re:',"
            " 'any update?', 'h-nudge', 'keep')",
            (USER_ID, now_iso()),
        )
        nudge = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO commitment_evidence (user_id, commitment_id, source_item_id,"
            " kind, seen_at) VALUES (?, ?, ?, 'restated', ?)",
            (USER_ID, cid, nudge, now_iso()),
        )
        assert questions._stale_commitments(conn, sett, MONDAY) == []

    def test_newest_evidence_is_chosen_by_instant_not_by_text_order(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The mixed-offset trap (2026-08-02): the two sightings straddle the silence
        floor (2026-08-10 for a 2026-08-24 run) with instant order and text order
        INVERTED. The Kolkata line "2026-08-11T01:00+05:30" is the textual max but
        the earlier instant (Aug 10 19:30Z); the Phoenix line "2026-08-10T23:50-07:00"
        is the true newest (Aug 11 06:50Z) and its stated local day sits ON the
        floor. Ranked by datetime() the newest sighting's day is 08-10 → silence
        holds and the question fires, citing that day; ranked by text the 08-11 line
        wins and the detector wrongly stays quiet."""
        cid = _open_commitment(
            conn, "send the transcript", due="2026-07-01", occurred="2026-06-20T10:00:00Z"
        )
        for ext, occurred in (
            ("m-kolkata", "2026-08-11T01:00:00+05:30"),
            ("m-phoenix", "2026-08-10T23:50:00-07:00"),
        ):
            conn.execute(
                "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
                " occurred_at, title, body_text, content_hash, triage_verdict)"
                " VALUES (?, 'apple-mail', ?, ?, ?, 'Re:', 'nudge', ?, 'keep')",
                (USER_ID, ext, now_iso(), occurred, f"h-{ext}"),
            )
            sid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            conn.execute(
                "INSERT INTO commitment_evidence (user_id, commitment_id, source_item_id,"
                " kind, seen_at) VALUES (?, ?, ?, 'restated', ?)",
                (USER_ID, cid, sid, now_iso()),
            )
        found = questions._stale_commitments(conn, sett, MONDAY)
        assert len(found) == 1 and found[0].subject_key == str(cid)
        assert "2026-08-10" in found[0].detail  # the true newest sighting is cited

    def test_merely_overdue_is_not_stale(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The board already nags inside two weeks; this detector is for decay."""
        _open_commitment(
            conn, "reply to advisor", due="2026-08-15", occurred="2026-08-14T10:00:00Z"
        )
        assert questions._stale_commitments(conn, sett, MONDAY) == []

    def test_a_flooded_board_is_asked_about_in_installments(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        for i in range(questions.STALE_BATCH_LIMIT + 3):
            _open_commitment(
                conn, f"dead favour {i}", due=f"2026-06-{10 + i:02d}",
                occurred="2026-06-01T10:00:00Z",
            )
        found = questions._stale_commitments(conn, sett, MONDAY)
        assert len(found) == questions.STALE_BATCH_LIMIT
        # Oldest due first: the longest-dead is asked about first.
        assert "dead favour 0" in found[0].question

    def test_the_answer_acts_through_the_boards_own_actions(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        cid = _open_commitment(
            conn, "send the transcript", due="2026-07-01", occurred="2026-06-20T10:00:00Z"
        )
        questions.refresh(conn, sett, MONDAY)
        qid = int(conn.execute(
            "SELECT id FROM open_question WHERE kind = 'stale'"
        ).fetchone()["id"])

        questions.answer(conn, sett, qid, option=questions.STALE_DONE)

        row = conn.execute("SELECT status, resolution_note FROM commitment WHERE id = ?",
                           (cid,)).fetchone()
        assert row["status"] == "done"
        assert "owner confirmed" in row["resolution_note"]

    def test_drop_drops_and_keep_keeps(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        first = _open_commitment(
            conn, "dead favour", due="2026-07-01", occurred="2026-06-20T10:00:00Z"
        )
        second = _open_commitment(
            conn, "still real", due="2026-07-02", occurred="2026-06-20T10:00:00Z"
        )
        questions.refresh(conn, sett, MONDAY)
        by_key = {
            str(r["subject_key"]): int(r["id"])
            for r in conn.execute("SELECT id, subject_key FROM open_question")
        }

        questions.answer(conn, sett, by_key[str(first)], option=questions.STALE_DROP)
        questions.answer(conn, sett, by_key[str(second)], option=questions.STALE_KEEP)

        statuses = {
            int(r["id"]): str(r["status"])
            for r in conn.execute("SELECT id, status FROM commitment")
        }
        assert statuses[first] == "dropped"
        assert statuses[second] == "open"

    def test_free_text_records_but_never_guesses(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The owner's own words are a complete answer AND not a license to act:
        "done I think, check with mom" is not a resolution."""
        cid = _open_commitment(
            conn, "dead favour", due="2026-07-01", occurred="2026-06-20T10:00:00Z"
        )
        questions.refresh(conn, sett, MONDAY)
        qid = int(conn.execute("SELECT id FROM open_question").fetchone()["id"])

        questions.answer(conn, sett, qid, text="done I think, check with mom")

        status = conn.execute("SELECT status FROM commitment WHERE id = ?", (cid,)).fetchone()
        assert status["status"] == "open"

    def test_an_answered_stale_question_is_not_reasked(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        _open_commitment(
            conn, "still real", due="2026-07-01", occurred="2026-06-20T10:00:00Z"
        )
        questions.refresh(conn, sett, MONDAY)
        qid = int(conn.execute("SELECT id FROM open_question").fetchone()["id"])
        questions.answer(conn, sett, qid, option=questions.STALE_KEEP)

        assert questions.refresh(conn, sett, MONDAY) == 0

    def test_a_commitment_closed_since_asking_is_a_no_op_answer(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        cid = _open_commitment(
            conn, "dead favour", due="2026-07-01", occurred="2026-06-20T10:00:00Z"
        )
        questions.refresh(conn, sett, MONDAY)
        qid = int(conn.execute("SELECT id FROM open_question").fetchone()["id"])
        from backglass.web import actions

        actions.resolve(conn, cid, note="board click")

        questions.answer(conn, sett, qid, option=questions.STALE_DROP)  # no raise
        row = conn.execute("SELECT status, resolution_note FROM commitment WHERE id = ?",
                           (cid,)).fetchone()
        assert row["status"] == "done"          # the board's click stands
        assert row["resolution_note"] == "board click"
