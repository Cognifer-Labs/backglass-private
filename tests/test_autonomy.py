"""No terminal required: the loops heal themselves, the buttons exist, paths volunteer.

The goal (2026-08-19): everything looped and happening every day, new roadmaps and
commitments auto-detected, questions only when necessary. Commitment detection has
been automatic since Phase 1; what this file pins is the rest — the schedule that
re-loads itself (a plist on disk is a wish; only a loaded label is a schedule), the
plan lifecycle on the web page instead of the CLI, and the roadmap that proposes
itself from the ledger's own evidence.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from backglass import questions, schedule
from backglass.config import Settings
from backglass.ledger import USER_ID

TODAY = date(2026, 8, 24)


def _open_commitment(conn: sqlite3.Connection, what: str) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, content_hash, triage_verdict)"
        " VALUES (?, 'apple-mail', ?, '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z',"
        " ?, ?, ?, 'keep')",
        (USER_ID, f"m-{what}", what, what, f"h-{what}"),
    )
    sid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, confidence, status,"
        " source_item_id, created_at)"
        " VALUES (?, 'i_owe', ?, 0.9, 'open', ?, '2026-08-10T00:00:00Z')",
        (USER_ID, what, sid),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


class TestTheScheduleHealsItself:
    """The 2026-08-18 failure shape: com.backglass.sync.plist on disk, log mtime
    fresh-looking, launchd never asked — and the ledger silently stopped for
    thirteen hours when the app closed."""

    def _fake_launchctl(
        self, monkeypatch: pytest.MonkeyPatch, *, loaded: list[str],
        bootstrap_fails: set[str] = frozenset(),
    ) -> list[list[str]]:
        calls: list[list[str]] = []

        class Proc:
            def __init__(self, rc: int, out: str = "") -> None:
                self.returncode = rc
                self.stdout = out
                self.stderr = ""

        def fake_run(argv: list[str], **_kw: object) -> Proc:
            calls.append(list(argv))
            if argv[:2] == ["launchctl", "list"]:
                rows = "\n".join(f"-\t0\t{label}" for label in loaded)
                return Proc(0, rows)
            if argv[:2] == ["launchctl", "bootstrap"]:
                label = Path(argv[3]).stem
                return Proc(1 if label in bootstrap_fails else 0)
            return Proc(1)

        import subprocess as sp

        monkeypatch.setattr(sp, "run", fake_run)
        return calls

    def _plists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, names: list[str]
    ) -> None:
        for name in names:
            (tmp_path / f"{name}.plist").write_text("<plist/>")
        monkeypatch.setattr(schedule, "LAUNCH_AGENTS_DIR", tmp_path)

    def test_an_unloaded_plist_is_found_and_bootstrapped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._plists(monkeypatch, tmp_path, ["com.backglass.sync", "com.backglass.brief"])
        calls = self._fake_launchctl(monkeypatch, loaded=["com.backglass.brief"])

        assert schedule.unloaded_jobs() == ["com.backglass.sync"]
        assert schedule.ensure_loaded() == ["com.backglass.sync"]
        bootstraps = [c for c in calls if c[:2] == ["launchctl", "bootstrap"]]
        assert len(bootstraps) == 1
        assert bootstraps[0][3].endswith("com.backglass.sync.plist")

    def test_a_fully_loaded_schedule_touches_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._plists(monkeypatch, tmp_path, ["com.backglass.sync"])
        calls = self._fake_launchctl(monkeypatch, loaded=["com.backglass.sync"])
        assert schedule.ensure_loaded() == []
        assert not [c for c in calls if c[:2] == ["launchctl", "bootstrap"]]

    def test_one_refusing_plist_does_not_cost_the_rest(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._plists(monkeypatch, tmp_path, ["com.backglass.brief", "com.backglass.sync"])
        self._fake_launchctl(
            monkeypatch, loaded=[], bootstrap_fails={"com.backglass.brief"}
        )
        assert schedule.ensure_loaded() == ["com.backglass.sync"]

    def test_no_launchctl_answers_unknown_never_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """None, not [] — an empty list reads as "everything is loaded" and a broken
        probe must not manufacture a green check (state's own founding rule)."""
        self._plists(monkeypatch, tmp_path, ["com.backglass.sync"])

        import subprocess as sp

        def boom(*_a: object, **_k: object) -> None:
            raise FileNotFoundError("launchctl")

        monkeypatch.setattr(sp, "run", boom)
        assert schedule.unloaded_jobs() is None
        assert schedule.ensure_loaded() == []

    def test_the_state_verdict_reads_it(
        self, conn: sqlite3.Connection, settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from backglass import state as state_mod

        monkeypatch.setattr(schedule, "unloaded_jobs", lambda: ["com.backglass.sync"])
        snapshot = state_mod.collect(conn, settings)
        checks = state_mod.verdicts(snapshot, conn, settings)
        verdict = next(v for v in checks if v.name == "every scheduled job is loaded")
        assert verdict.ok is False and "com.backglass.sync" in verdict.detail

        monkeypatch.setattr(schedule, "unloaded_jobs", lambda: [])
        checks = state_mod.verdicts(state_mod.collect(conn, settings), conn, settings)
        assert next(
            v for v in checks if v.name == "every scheduled job is loaded"
        ).ok is True


class TestThePlanLifecycleOnThePage:
    """Accepting and rebuilding a plan were CLI flags; the replan boundary (proposed
    is the system's, accepted is the owner's) is meaningless if crossing it needs a
    terminal."""

    @pytest.fixture
    def client(self, conn: sqlite3.Connection, settings: Settings):  # type: ignore[no-untyped-def]
        from fastapi.testclient import TestClient

        from backglass.web.app import create_app

        del conn
        return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")

    def _plan(self, conn: sqlite3.Connection, settings: Settings, day: date) -> int:
        from backglass.plan import planner

        _open_commitment(conn, "anything to plan")
        sett = settings.model_copy(update={
            "working_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        })
        pid = planner.persist(conn, sett, planner.propose(conn, sett, day))
        conn.commit()
        return pid

    def test_accept_is_a_button(
        self, conn: sqlite3.Connection, settings: Settings, client  # type: ignore[no-untyped-def]
    ) -> None:
        pid = self._plan(conn, settings, TODAY)

        page = client.get(f"/schedule?date={TODAY.isoformat()}").text
        assert "Accept plan" in page

        posted = client.post(
            f"/schedule/{TODAY.isoformat()}/accept", follow_redirects=False
        )
        assert posted.status_code == 303
        row = conn.execute("SELECT status, accepted_at FROM day_plan WHERE id = ?",
                           (pid,)).fetchone()
        assert row["status"] == "accepted" and row["accepted_at"]
        # The page now says so, and stops offering Accept.
        page = client.get(f"/schedule?date={TODAY.isoformat()}").text
        assert "plan accepted" in page
        assert "Accept plan" not in page

    def test_replan_is_a_button_and_supersedes(
        self, conn: sqlite3.Connection, settings: Settings, client  # type: ignore[no-untyped-def]
    ) -> None:
        pid = self._plan(conn, settings, TODAY)
        posted = client.post(
            f"/schedule/{TODAY.isoformat()}/replan", follow_redirects=False
        )
        assert posted.status_code == 303
        old = conn.execute("SELECT status FROM day_plan WHERE id = ?", (pid,)).fetchone()
        assert old["status"] == "superseded"
        live = conn.execute(
            "SELECT COUNT(*) AS n FROM day_plan WHERE status != 'superseded'"
        ).fetchone()
        assert live["n"] == 1

    def test_an_unplanned_day_offers_to_plan_itself(
        self, conn: sqlite3.Connection, settings: Settings, client  # type: ignore[no-untyped-def]
    ) -> None:
        del conn, settings
        page = client.get(f"/schedule?date={TODAY.isoformat()}").text
        assert "Plan this day" in page


class TestRoadmapsVolunteer:
    def test_three_signal_hits_raise_the_question(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _open_commitment(conn, "submit MCAT registration")
        _open_commitment(conn, "email hospice volunteer coordinator")
        _open_commitment(conn, "set up Anki deck for bio")

        found = questions._roadmap_candidates(conn, settings, TODAY)
        assert [q.subject_key for q in found] == ["medical"]
        q = found[0]
        assert q.kind == "roadmap"
        assert questions.ROADMAP_START in q.options
        assert "Done means:" in q.detail
        # Detection is read-only — nothing instantiated by asking.
        assert conn.execute("SELECT COUNT(*) AS n FROM roadmap").fetchone()["n"] == 0

    def test_two_hits_stay_silent(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """"Only ask questions if necessary": one errand is an errand."""
        _open_commitment(conn, "submit MCAT registration")
        _open_commitment(conn, "email hospice volunteer coordinator")
        assert questions._roadmap_candidates(conn, settings, TODAY) == []

    def test_a_tracked_path_never_volunteers_again(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Any roadmap row — active, done, or DROPPED — means the owner decided."""
        for what in ("submit MCAT registration", "email hospice volunteer coordinator",
                     "set up Anki deck for bio"):
            _open_commitment(conn, what)
        conn.execute(
            "INSERT INTO goal (user_id, title, horizon, definition_of_done, status,"
            " created_at)"
            " VALUES (?, 'x', 'annual', 'done', 'active', '2026-08-01T00:00:00Z')",
            (USER_ID,),
        )
        gid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO roadmap (user_id, path_id, path_version, title, goal_id,"
            " status, created_at)"
            " VALUES (?, 'medical', 'medical@1', 'x', ?, 'dropped',"
            " '2026-08-01T00:00:00Z')",
            (USER_ID, gid),
        )
        assert questions._roadmap_candidates(conn, settings, TODAY) == []

    def test_the_start_answer_instantiates_the_roadmap(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        for what in ("submit MCAT registration", "email hospice volunteer coordinator",
                     "set up Anki deck for bio"):
            _open_commitment(conn, what)
        questions.refresh(conn, settings, TODAY)
        qid = int(conn.execute(
            "SELECT id FROM open_question WHERE kind = 'roadmap'"
        ).fetchone()["id"])

        questions.answer(conn, settings, qid, option=questions.ROADMAP_START)

        roadmap = conn.execute("SELECT path_id, status FROM roadmap").fetchone()
        assert roadmap["path_id"] == "medical" and roadmap["status"] == "active"
        assert conn.execute("SELECT COUNT(*) AS n FROM roadmap_step").fetchone()["n"] > 0
        # And it never re-asks: the path is tracked now.
        assert questions._roadmap_candidates(conn, settings, TODAY) == []

    def test_declining_creates_nothing_and_never_reasks(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        for what in ("submit MCAT registration", "email hospice volunteer coordinator",
                     "set up Anki deck for bio"):
            _open_commitment(conn, what)
        questions.refresh(conn, settings, TODAY)
        qid = int(conn.execute(
            "SELECT id FROM open_question WHERE kind = 'roadmap'"
        ).fetchone()["id"])

        questions.answer(conn, settings, qid, option=questions.ROADMAP_NOT_THIS)

        assert conn.execute("SELECT COUNT(*) AS n FROM roadmap").fetchone()["n"] == 0
        assert questions.refresh(conn, settings, TODAY) == 0

    def test_signal_words_match_on_boundaries_not_substrings(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """"anki" must not fire inside "banking" (2026-07-30 banned-word lesson)."""
        _open_commitment(conn, "sort out banking paperwork")
        _open_commitment(conn, "more banking things")
        _open_commitment(conn, "banking again and again")
        assert questions._roadmap_candidates(conn, settings, TODAY) == []
