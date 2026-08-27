"""The repair loop as a thing that can be called, and that says when it broke.

Before `backglass/repair.py` the chain was ninety lines inside a Typer command with seven
bare `except Exception: pass` blocks. That shape has three failures, and there is a test
here for each: it ran from one entry point, it could not be tested at all, and a step that
had been raising for a week was indistinguishable from a ledger with nothing to repair.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backglass import repair
from backglass.config import Settings
from backglass.ledger import USER_ID

DAY = date(2026, 8, 24)
#: A fixed instant for the on-open slot, which records when a pass ran. Pinned so
#: the assertion is about the report and not about the hour the suite was run at.
_NOW = datetime(2026, 8, 24, 9, 15, tzinfo=ZoneInfo("America/Phoenix"))


@pytest.fixture
def sett(settings: Settings) -> Settings:
    return settings.model_copy(update={"default_tz": "America/Phoenix"})


def _step(name: str, *, lines: list[str] | None = None, boom: bool = False) -> repair.Step:
    def run(conn: sqlite3.Connection, settings: Settings, day: date) -> list[str]:
        del conn, settings, day
        if boom:
            raise RuntimeError(f"{name} fell over")
        return list(lines or [])

    return repair.Step(name, run)


class TestTheChain:
    def test_the_real_chain_runs_end_to_end_on_an_empty_ledger(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Every step, in the documented order, against a ledger with nothing in it —
        which is the branch each step's "nothing to do" path is written for."""
        report = repair.run(conn, sett, day=DAY)

        assert report.ran == [step.name for step in repair.STEPS]
        assert report.errors == []

    def test_one_step_falling_over_costs_that_step_and_no_other(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Rule 5's unit is the step. This is the one thing the seven bare `except: pass`
        blocks got right, and it has to survive the refactor that made them visible."""
        steps = (
            _step("first", lines=["did a thing"]),
            _step("broken", boom=True),
            _step("third", lines=["did another"]),
        )

        report = repair.run(conn, sett, day=DAY, steps=steps)

        assert report.ran == ["first", "third"]
        assert report.lines == ["did a thing", "did another"]
        assert report.errors == ["broken: RuntimeError: broken fell over"]

    def test_the_failure_names_the_step(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """"RuntimeError: fell over" in a log is not actionable. Which repair stopped
        working is the whole content of the message."""
        report = repair.run(conn, sett, day=DAY, steps=(_step("notify", boom=True),))

        assert report.errors[0].startswith("notify: ")

    def test_the_logic_checkers_own_errors_are_surfaced(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`logic.Report.errors` was collected by the checker and dropped by its caller:
        one rule could raise on every pass forever and nothing anywhere would say so."""
        from backglass import logic as logic_mod

        class _Report:
            applied = 0
            errors = ["a rule fell over"]

            def by_rule(self) -> dict[str, int]:
                return {}

        monkeypatch.setattr(logic_mod, "run", lambda *a, **k: _Report())
        logic_step = next(s for s in repair.STEPS if s.name == "logic")

        report = repair.run(conn, sett, day=DAY, steps=(logic_step,))

        assert any("a rule fell over" in line for line in report.lines)


class TestOverdue:
    def _a_run(self, conn: sqlite3.Connection, finished_at: str, kind: str = "sync") -> None:
        conn.execute(
            "INSERT INTO run (user_id, started_at, finished_at, kind) VALUES (?, ?, ?, ?)",
            (USER_ID, finished_at, finished_at, kind),
        )

    def test_a_ledger_that_has_never_synced_is_not_overdue(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """A fresh install must not start a planner on its first page view: there is
        nothing to repair yet."""
        assert repair.is_overdue(conn, sett) is False

    def test_a_recent_sync_is_not_overdue(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        from backglass.plan import timezones

        now = timezones.local_now(sett)
        self._a_run(conn, (now - timedelta(minutes=10)).isoformat())

        assert repair.is_overdue(conn, sett, now=now) is False

    def test_a_loop_that_has_not_run_in_an_hour_is(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The case this exists for. On 2026-08-17 the morning jobs had been 12h30 late
        for weeks, because the agent that evaluates calendar intervals holds the timezone
        the machine booted in."""
        from backglass.plan import timezones

        now = timezones.local_now(sett)
        self._a_run(conn, (now - timedelta(minutes=60)).isoformat())

        assert repair.is_overdue(conn, sett, now=now) is True

    def test_a_run_of_another_kind_does_not_answer_this_question(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Only sync rows carry the loop. A batch collection finishing is not the loop
        having run, and reading the newest row of any kind would say that it was."""
        from backglass.plan import timezones

        now = timezones.local_now(sett)
        self._a_run(conn, (now - timedelta(minutes=90)).isoformat())
        self._a_run(conn, now.isoformat(), kind="batch")

        assert repair.is_overdue(conn, sett, now=now) is True

    def test_an_unreadable_timestamp_is_not_overdue(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """This runs on a full-page load. A row it cannot parse must not raise into the
        page, and must not claim the loop is late on no evidence."""
        self._a_run(conn, "not a timestamp")

        assert repair.is_overdue(conn, sett) is False


class TestOnOpen:
    def test_a_lock_held_by_another_process_is_a_skip_not_a_failure(
        self, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Another run is already doing this work — the same conclusion `sync` reaches.
        Two passes over one day means a second proposal immediately superseded and both
        of them paid for.

        Raised rather than actually held: `run_lock` is re-entrant *within* a process
        (it counts holders in `_HELD`) and only excludes across processes via flock, so a
        second thread here would re-enter and the test would assert nothing. What is
        being tested is `on_open`'s handling of the refusal, and that is what this
        provokes.
        """
        from backglass import sync as sync_mod

        def locked(*_a: object, **_k: object) -> None:
            raise sync_mod.SyncLocked("another run holds it")

        monkeypatch.setattr(repair, "_lock", locked, raising=False)
        monkeypatch.setattr(sync_mod, "run_lock", locked)

        report = repair.on_open(sett)

        assert report.skipped is True
        assert report.ran == []

    def test_it_runs_the_chain_on_its_own_connection_and_commits(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The app-open path has no connection of its own to borrow — this is what makes
        opening the dashboard a trigger rather than a read."""
        del conn  # the fixture's job here is to have migrated settings.db_path

        report = repair.on_open(sett)

        assert report.skipped is False
        assert report.ran == [step.name for step in repair.STEPS]


class TestTheReportReachesASurface:
    """`on_open` writes no `run` row on purpose, so until 2026-08-25 its report went to
    stderr — a stream the desktop shell does not have. Rule 5 asks a failing part to
    degrade, be logged AND be surfaced; only the third was missing, and only on this path.
    """

    @pytest.fixture(autouse=True)
    def _clean_slot(self) -> None:
        """The slot is process-global. A test that left one behind would make the next
        one assert on another test's pass."""
        repair._record(repair.Report(), now=_NOW)
        with repair._last_lock:
            repair._last = None

    def test_nothing_has_run_reads_as_nothing_not_as_health(self) -> None:
        """A just-started process has repaired nothing, which is a different fact from
        having nothing to repair. The alert must not fire on either, but the reader must
        be able to tell them apart."""
        assert repair.last() is None

    def test_a_pass_is_recorded_with_the_time_it_ran(self) -> None:
        report = repair.Report(lines=["plan replaced"], ran=["replan"])
        repair._record(report, now=_NOW)

        recorded = repair.last()
        assert recorded is not None
        at, held = recorded
        assert at == _NOW
        assert held.lines == ["plan replaced"]

    def test_a_failed_pass_raises_a_vermilion_alert_naming_the_step(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        from backglass.web import panels
        from tests.conftest import healthy_run, todays_plan

        healthy_run(conn)
        todays_plan(conn, sett)
        conn.commit()

        repair._record(
            repair.Report(errors=["logic: RuntimeError: no such column"]), now=_NOW
        )
        sidebar = panels.sidebar(conn, sett, DAY)

        alert = next(
            (a for a in sidebar.alerts if a["text"].startswith("repair failed")), None
        )
        assert alert is not None, [a["text"] for a in sidebar.alerts]
        assert alert["level"] == "verm"
        assert "logic: RuntimeError: no such column" in alert["text"]

    def test_further_failures_are_counted_rather_than_dropped(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """One line in a 236px column. The others are counted, never silently discarded —
        an alert that shows one of three failures and says so is honest; one that shows
        one of three and implies it is the only one is not."""
        from backglass.web import panels
        from tests.conftest import healthy_run, todays_plan

        healthy_run(conn)
        todays_plan(conn, sett)
        conn.commit()

        repair._record(
            repair.Report(errors=["logic: boom", "notify: boom", "vault: boom"]),
            now=_NOW,
        )
        alert = next(
            a for a in panels.sidebar(conn, sett, DAY).alerts
            if a["text"].startswith("repair failed")
        )
        assert "(+2 more)" in alert["text"]

    def test_the_spawned_thread_is_what_fills_the_slot(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The path that actually runs in the app, end to end.

        Every other test here calls `_record` directly, which proves the slot and the
        alert and not the wiring between them — a slot nothing fills reads exactly like a
        healthy installation. This drives `spawn_on_open`, which is what the dashboard
        calls, and waits for the daemon thread to land its report.
        """
        import time

        del conn  # the fixture's job is to have migrated settings.db_path

        monkeypatch.setattr(repair, "STEPS", (_step("logic", boom=True),))

        repair.spawn_on_open(sett)

        deadline = time.monotonic() + 10
        while repair.last() is None and time.monotonic() < deadline:
            time.sleep(0.02)

        recorded = repair.last()
        assert recorded is not None, "the spawned thread never recorded its pass"
        assert recorded[1].errors == ["logic: RuntimeError: logic fell over"]

    def test_a_clean_pass_raises_no_alert(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The loop runs on every open that finds it overdue. An alert per pass would be
        furniture inside a day, and furniture is what stops alerts being read."""
        from backglass.web import panels
        from tests.conftest import healthy_run, todays_plan

        healthy_run(conn)
        todays_plan(conn, sett)
        conn.commit()

        repair._record(repair.Report(lines=["nothing to do"], ran=["logic"]), now=_NOW)

        assert not [
            a for a in panels.sidebar(conn, sett, DAY).alerts
            if a["text"].startswith("repair failed")
        ]
