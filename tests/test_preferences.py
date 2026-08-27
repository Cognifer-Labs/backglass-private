"""The owner's stated priorities reach the planner — as a tiebreak, never a coup.

`preferences/planner.priority` is a fact like any other; plan/preferences.py parses it
into lanes and `planner.order` uses the lane INSIDE the date-derived priority band.
The invariants: dates still dominate, malformed values degrade loudly to no
preference, and the school-vs-gym collision becomes a question rather than a guess.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from backglass import questions
from backglass.config import Settings
from backglass.facts import remember
from backglass.ledger import USER_ID
from backglass.plan import planner, preferences
from backglass.plan.planner import Candidate

MONDAY = date(2026, 8, 24)


def _cand(what: str, *, priority: int = 4, minutes: int = 45, **over: object) -> Candidate:
    base: dict[str, object] = {
        "commitment_id": hash(what) % 100000,
        "what": what,
        "due_at": None,
        "minutes": minutes,
        "direction": "i_owe",
        "goal_id": None,
        "rollover_count": 0,
        "priority": priority,
        "blocked_on_others": False,
        "age_days": 0,
    }
    base.update(over)
    return Candidate(**base)  # type: ignore[arg-type]


class TestTheParse:
    def test_lanes_with_keywords_and_bare_lanes(self) -> None:
        prefs = preferences.parse("school: class, bio, homework > premed: mcat > gym")
        assert [lane.name for lane in prefs.lanes] == ["school", "premed", "gym"]
        assert prefs.lanes[0].keywords == ("class", "bio", "homework")
        assert prefs.lanes[2].keywords == ("gym",)  # bare lane matches its own name
        assert prefs.warnings == ()

    def test_rank_matches_on_word_boundaries(self) -> None:
        prefs = preferences.parse("art: art > rest")
        assert prefs.rank("finish art project") == 0
        # "art" inside "quarterly" must not fire (banned-word lesson, 2026-07-30).
        assert prefs.rank("file quarterly report") == 2

    def test_unmatched_sorts_after_matched_lanes(self) -> None:
        prefs = preferences.parse("school: bio")
        assert prefs.rank("bio 181 problem set") == 0
        assert prefs.rank("water the plants") == 1

    def test_garbage_degrades_loudly_to_no_preference(self) -> None:
        prefs = preferences.parse(":::>>>")
        assert prefs.lanes == ()
        assert prefs.warnings  # said so, not silent
        # And an empty preference ranks everything equal.
        assert prefs.rank("anything") == 0

    def test_absence_loads_the_stated_default_and_a_fact_overrides_it(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Changed 2026-08-24. Absence used to be *inert*, which sounded careful and meant
        the priority mechanism had never once changed an outcome: the fact was never
        written, so every commitment ranked identically inside its date band. The owner's
        ruled list (`plan/priority.py`) is the default now; the fact still overrides it."""
        from backglass.plan import priority

        default = preferences.load(conn).lanes
        assert default == priority.lanes().lanes
        assert default, "the default is a list, not an empty one"

        remember(conn, settings, "preferences", "planner.priority", "school: bio > social")
        assert [lane.name for lane in preferences.load(conn).lanes] == ["school", "social"]


class TestTheOrdering:
    def test_within_a_band_the_stated_lane_wins(self) -> None:
        prefs = preferences.parse("school: bio > social: dinner")
        social = _cand("plan dinner with Sam", age_days=30)  # older — would win on age
        school = _cand("bio 181 problem set", age_days=1)

        ordered = planner.order([social, school], prefs)
        assert [c.what for c in ordered] == ["bio 181 problem set", "plan dinner with Sam"]

    def test_a_preference_cannot_outrank_a_due_date(self) -> None:
        """The tiebreak sits INSIDE the priority band: school beating social must not
        plan next week's homework over today's overdue favour."""
        prefs = preferences.parse("school: bio > social: dinner")
        overdue_social = _cand("plan dinner with Sam", priority=0)
        future_school = _cand("bio 181 problem set", priority=4)

        ordered = planner.order([overdue_social, future_school], prefs)
        assert ordered[0].what == "plan dinner with Sam"

    def test_no_preference_is_byte_identical_to_before(self) -> None:
        a = _cand("alpha", age_days=3)
        b = _cand("beta", age_days=9)
        assert planner.order([a, b]) == planner.order([a, b], preferences.Preferences())


class TestTheWiredDoor:
    def _commitment(
        self, conn: sqlite3.Connection, what: str, *, minutes: int = 60
    ) -> int:
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, title, body_text, content_hash, triage_verdict)"
            " VALUES (?, 'manual', ?, '2026-08-20T00:00:00Z', '2026-08-20T00:00:00Z',"
            " ?, ?, ?, 'keep')",
            (USER_ID, f"x-{what}", what, what, f"h-{what}"),
        )
        sid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO commitment (user_id, direction, what, confidence, status,"
            " estimated_minutes, estimate_source, source_item_id, created_at)"
            " VALUES (?, 'i_owe', ?, 0.9, 'open', ?, 'manual', ?, '2026-08-20T00:00:00Z')",
            (USER_ID, what, minutes, sid),
        )
        return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def test_the_preference_fact_reorders_a_real_proposal(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Drive propose(), not order() — the door callers actually use."""
        sett = settings.model_copy(update={
            "working_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        })
        self._commitment(conn, "plan dinner with Sam")
        self._commitment(conn, "bio 181 problem set")
        remember(conn, sett, "preferences", "planner.priority", "school: bio > social: dinner")

        proposal = planner.propose(conn, sett, MONDAY)
        work = [b["title"] for b in proposal.blocks if b["kind"] in ("work", "protected")]
        assert work.index("bio 181 problem set") < work.index("plan dinner with Sam")

    def test_a_malformed_preference_is_said_in_the_plan_notes(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        sett = settings.model_copy(update={
            "working_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        })
        self._commitment(conn, "anything at all")
        remember(conn, sett, "preferences", "planner.priority", ":::>>>")

        proposal = planner.propose(conn, sett, MONDAY)
        assert any("planner.priority" in n for n in proposal.notes)


class TestProtectedConflicts:
    def _due_today(
        self, conn: sqlite3.Connection, what: str, *, minutes: int
    ) -> int:
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, title, body_text, content_hash, triage_verdict)"
            " VALUES (?, 'manual', ?, '2026-08-20T00:00:00Z', '2026-08-20T00:00:00Z',"
            " ?, ?, ?, 'keep')",
            (USER_ID, f"x-{what}", what, what, f"h-{what}"),
        )
        sid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO commitment (user_id, direction, what, due_at, confidence,"
            " status, estimated_minutes, estimate_source, source_item_id, created_at)"
            " VALUES (?, 'i_owe', ?, ?, 0.9, 'open', ?, 'manual', ?,"
            " '2026-08-20T00:00:00Z')",
            (USER_ID, what, MONDAY.isoformat(), minutes, sid),
        )
        return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    def _squeezed(self, settings: Settings) -> Settings:
        """A day almost consumed by a gym routine: 08:00–10:00 window, 90m gym."""
        return settings.model_copy(update={
            "working_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "working_window": "08:00-10:00",
            "routines": "gym@08:00+90",
        })

    def test_school_versus_gym_becomes_a_question(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        sett = self._squeezed(settings)
        cid = self._due_today(conn, "bio 181 problem set", minutes=60)

        found = questions._protected_conflicts(conn, sett, MONDAY)
        assert len(found) == 1
        q = found[0]
        assert q.kind == "priority"
        assert q.subject_key == f"protected|{cid}|gym"
        assert "Gym holds 90m" in q.question
        assert questions.PROTECTED_GIVE in q.options
        # Recognition is read-only: the routine still stands in the ledger's plan.
        assert conn.execute("SELECT COUNT(*) AS n FROM day_plan").fetchone()["n"] == 0

    def test_a_fitting_day_asks_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        sett = self._squeezed(settings).model_copy(update={"working_window": "08:00-14:00"})
        self._due_today(conn, "bio 181 problem set", minutes=60)
        assert questions._protected_conflicts(conn, sett, MONDAY) == []

    def test_a_routine_too_short_to_help_is_not_blamed(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The overflowed item needs 100m; gym holds 90m. Giving up the routine would
        not fit the item, so there is no decision to ask for."""
        sett = self._squeezed(settings)
        self._due_today(conn, "bio 181 problem set", minutes=100)
        assert questions._protected_conflicts(conn, sett, MONDAY) == []

    def test_the_same_weekly_collision_is_one_question(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Ask-once identity is (commitment, routine), not the day — a weekly gym slot
        against the same problem set is one question, not one per week."""
        sett = self._squeezed(settings)
        self._due_today(conn, "bio 181 problem set", minutes=60)

        assert questions.refresh(conn, sett, MONDAY) >= 1
        next_week = date(2026, 8, 31)
        assert questions.refresh(conn, sett, next_week) == 0
