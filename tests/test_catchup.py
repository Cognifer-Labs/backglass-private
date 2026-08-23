"""The net under the morning jobs, and the clock the planner reads.

Audited 2026-08-15: `com.backglass.plan` is scheduled for 05:45 and last ran at 17:22;
`com.backglass.brief` is scheduled for 06:00 and last ran at 17:37. launchd defers a
missed calendar interval to the next wake and the machine sleeps through both hours, so
the plan for a day arrived after the day and the two-minute morning read arrived at
dinner. `plan-catchup` did not cover it — `RunAtLoad` is login, not lid-open wake.

Two halves, tested here:

  - `catchup.run`, hung off the every-1800s sync job, which is the one scheduled job that
    does run on wake. It fills a hole and never replaces anything.
  - the planner's `now`, which makes a deferred run plan the hours that are left instead
    of packing a morning that has already gone.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backglass import catchup
from backglass.config import Settings
from backglass.plan import capacity as capacity_mod
from backglass.plan import planner

PHOENIX = ZoneInfo("America/Phoenix")
#: A Tuesday, so it is a working day under the default working_days.
DAY = date(2026, 8, 18)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, hour, minute, tzinfo=PHOENIX)


def _commitment(conn: sqlite3.Connection, what: str, *, minutes: int = 45) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, content_hash, triage_verdict, extraction_version)"
        " VALUES (1, 'manual', ?, '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z',"
        " ?, ?, ?, 'keep', 'manual')",
        (f"x{what}", what, what, f"h{what}"),
    )
    item = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, confidence, status,"
        " estimated_minutes, estimate_source, source_item_id, created_at)"
        " VALUES (1, 'i_owe', ?, 0.9, 'open', ?, 'manual', ?, '2026-08-10T00:00:00Z')",
        (what, minutes, item),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


class TestTheClampedWindow:
    def test_a_run_before_the_window_plans_the_whole_window(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """05:45 is before a 09:00 window opens, so nothing is spent and nothing clamps.
        This is the normal path and it must not change."""
        early = capacity_mod.compute(conn, settings, DAY, events=[], not_before=_at(5, 45))
        whole = capacity_mod.compute(conn, settings, DAY, events=[])
        assert early.window_minutes == whole.window_minutes

    def test_a_deferred_run_does_not_sell_the_morning_twice(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The 17:22 case. Hours that have already happened are not capacity."""
        whole = capacity_mod.compute(conn, settings, DAY, events=[])
        late = capacity_mod.compute(conn, settings, DAY, events=[], not_before=_at(16, 0))
        assert late.window_minutes < whole.window_minutes
        assert late.capacity_minutes < whole.capacity_minutes

    def test_no_block_is_placed_before_the_clock(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The finding, stated as a test: a plan built at 16:00 that opens with a 09:30
        block is a historical document, not a plan."""
        _commitment(conn, "call the volunteer coordinator")
        proposal = planner.propose(conn, settings, DAY, events=[], now=_at(16, 0))
        placed = [
            b for b in proposal.blocks if b["commitment_id"] is not None
        ]
        assert placed, "something should still be schedulable in the evening"
        for block in placed:
            assert datetime.fromisoformat(block["starts_at"]) >= _at(16, 0)

    def test_omitting_now_keeps_the_whole_day(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Tests and what-ifs measure a deterministic window; only callers that know the
        wall clock opt in."""
        _commitment(conn, "submit the volunteer application")
        proposal = planner.propose(conn, settings, DAY, events=[])
        assert proposal.capacity.window_minutes == capacity_mod.compute(
            conn, settings, DAY, events=[]
        ).window_minutes

    def test_a_clock_on_another_day_does_not_clamp(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Planning tomorrow at 16:00 today plans all of tomorrow."""
        tomorrow = DAY + timedelta(days=1)
        proposal = planner.propose(conn, settings, tomorrow, events=[], now=_at(16, 0))
        assert proposal.capacity.window_minutes == capacity_mod.compute(
            conn, settings, tomorrow, events=[]
        ).window_minutes


class TestTheNet:
    def test_it_says_nothing_before_the_hour(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Before 05:45 nothing is missing — the morning has not happened yet, and
        generating a plan then would be the net pre-empting the job it backs up."""
        assert catchup.run(conn, settings, now=_at(4, 0)) == []

    def test_it_fills_a_plan_the_sleeping_machine_never_got(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _commitment(conn, "email the signed waivers")
        filled = catchup.run(conn, settings, now=_at(8, 0))
        assert [f.surface for f in filled] == ["plan", "brief"]
        assert planner.current_plan_id(conn, DAY) is not None

    def test_it_never_replaces_a_plan_that_exists(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The property that makes it safe on a 30-minute timer. An accepted or
        hand-edited plan must survive every sync of the day."""
        catchup.run(conn, settings, now=_at(8, 0))
        first = planner.current_plan_id(conn, DAY)
        conn.execute(
            "UPDATE day_plan SET status = 'accepted', accepted_at = '2026-08-18T08:05:00'"
            " WHERE id = ?",
            (first,),
        )
        assert catchup.run(conn, settings, now=_at(9, 0)) == []
        assert planner.current_plan_id(conn, DAY) == first

    def test_it_generates_the_brief_but_does_not_send_it(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Sending is an outbound act with an hour attached to it. The hole this fills is
        that the brief did not exist to be read at all."""
        catchup.run(conn, settings, now=_at(8, 0))
        row = conn.execute(
            "SELECT sent_at FROM brief WHERE generated_for_date = ? AND kind = 'daily'",
            (DAY.isoformat(),),
        ).fetchone()
        assert row is not None
        assert row["sent_at"] is None

    def test_a_second_sync_writes_no_second_brief(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Rule 3, in the form that matters here: nobody receives two briefs."""
        catchup.run(conn, settings, now=_at(8, 0))
        assert catchup.run(conn, settings, now=_at(8, 30)) == []
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM brief WHERE generated_for_date = ? AND kind = 'daily'",
            (DAY.isoformat(),),
        ).fetchone()["n"]
        assert count == 1

    def test_a_failure_degrades_and_does_not_raise(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rule 5. This runs inside the sync job; a brief that cannot be built must not
        take the sync down with it."""
        def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("no")

        monkeypatch.setattr("backglass.catchup._fill_plan", boom)
        filled = catchup.run(conn, settings, now=_at(8, 0))
        assert any(f.surface == "plan" and "failed" in f.detail for f in filled)


class TestTheHourIsConfiguredOnce:
    def test_the_plan_template_renders_the_setting(self) -> None:
        """The 05:45 was frozen in the template while `brief_at` came from settings —
        the exact drift `schedule.render` exists to stop, and now also the hour the net
        measures "the morning already passed" against."""
        import plistlib
        from pathlib import Path

        from backglass import schedule

        rendered = schedule.render(
            Path("/r"), "/u", Path("/h"), Settings(_env_file=None, plan_at="04:30")
        )
        parsed = plistlib.loads(rendered["com.backglass.plan.plist"].encode())
        assert parsed["StartCalendarInterval"] == {"Hour": 4, "Minute": 30}

    def test_the_net_reads_the_same_setting(self, settings: Settings) -> None:
        late = settings.model_copy(update={"plan_at": "23:00"})
        assert not catchup._owed(_at(22, 0), late.plan_at)
        assert catchup._owed(_at(23, 0), late.plan_at)


class TestTheAppOpenTrigger:
    """2026-08-17: the owner asked why the day was not planned. It was — at 08:18, by the
    30-minute sync's net — but the 05:45 job had not fired on time for weeks. The Mac last
    booted in Kolkata and `com.apple.UserEventAgent-Aqua` reads the timezone once at
    start, so every calendar job was being evaluated against IST: 05:45 → 17:15, 22:00 →
    09:30. Reloading a job does not clear it, SIP refuses to restart that agent, and only a
    reboot does — so the trigger the owner controls, opening the app, gets a hook.
    """

    def test_a_hole_is_only_a_hole_after_the_hour(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        assert not catchup.hole_exists(conn, settings, now=_at(4, 0))
        assert catchup.hole_exists(conn, settings, now=_at(8, 0))

    def test_an_early_look_does_not_suppress_a_later_one(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The bug a "checked today already" marker would have: nothing is missing at
        04:00 because nothing is owed yet, and a marker stamped then would skip the 09:00
        check that finds the real hole."""
        assert not catchup.hole_exists(conn, settings, now=_at(4, 0))
        assert catchup.hole_exists(conn, settings, now=_at(9, 0))

    def test_a_filled_day_is_not_a_hole(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        catchup.run(conn, settings, now=_at(8, 0))
        assert not catchup.hole_exists(conn, settings, now=_at(8, 30))

    def test_it_does_not_ask_the_embedding_endpoint(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Retrieval is additive by CLAUDE.md's ruling; the request path must not depend on
        it being reachable. The backlog rides along on a real fill and on the sync."""
        from backglass import search

        def boom(*_a: object, **_k: object) -> int:
            raise AssertionError("the page asked ollama")

        monkeypatch.setattr(search, "index", boom)
        assert catchup.hole_exists(conn, settings, now=_at(8, 0))

    # `on_open` and `spawn_on_open` moved to `backglass/loop.py` when opening the app
    # started running the whole loop rather than this one pass; their tests moved with
    # them to tests/test_loop.py::TestTheAppOpenTrigger. `hole_exists` stayed here,
    # because it is the cheap question about *this* net.


class TestTheBriefHoleClosesOnEveryDayOfTheWeek:
    """Found on the live ledger while wiring the app-open trigger: `data/sync.log` had two
    "caught up brief for 2026-08-17" lines thirty minutes apart, and the ledger had one
    brief row for that day with `kind = 'monday'`.

    `build_for` returns the Monday brief on a week-start day — it replaces the daily one,
    docs/04 §2.7 W1 — and a Friday retro is stored as `kind = 'friday'`. The net's check
    asked for `kind = 'daily'`, so on those days the hole never closed and every sync
    rebuilt the brief. A per-page-load trigger would have made that every page load.
    """

    def test_a_monday_brief_closes_the_hole(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        monday = date(2026, 8, 17)
        assert monday.weekday() == 0
        conn.execute(
            "INSERT INTO brief (user_id, generated_for_date, kind, content_md, items_json,"
            " word_count) VALUES (1, ?, 'monday', '# monday', '[]', 3)",
            (monday.isoformat(),),
        )
        assert not catchup.brief_is_missing(conn, monday)

    def test_a_friday_retro_closes_the_hole(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        friday = date(2026, 8, 21)
        assert friday.weekday() == 4
        conn.execute(
            "INSERT INTO brief (user_id, generated_for_date, kind, content_md, items_json,"
            " word_count) VALUES (1, ?, 'friday', '# friday', '[]', 3)",
            (friday.isoformat(),),
        )
        assert not catchup.brief_is_missing(conn, friday)

    def test_a_week_start_day_is_caught_up_exactly_once(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole failure, end to end: run the net twice on a Monday morning and the
        second pass must find nothing to do."""
        from backglass.brief import weekly

        monkeypatch.setattr(weekly, "is_week_start", lambda *_a, **_k: True)
        first = catchup.run(conn, settings, now=_at(8, 0))
        assert "brief" in [f.surface for f in first]
        assert catchup.run(conn, settings, now=_at(8, 30)) == []
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM brief WHERE generated_for_date = ?",
            (DAY.isoformat(),),
        ).fetchone()["n"]
        assert count == 1

    def test_it_is_still_missing_when_nothing_was_written(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        assert catchup.brief_is_missing(conn, DAY)


class TestTheDashboardAsks:
    """The wiring, tested at the seam rather than through a real planner: `create_app`
    takes the trigger as an argument, so a test can count the calls and no test can
    accidentally start background work against its fixture ledger."""

    def _client(self, settings: Settings, calls: list[int]):  # type: ignore[no-untyped-def]
        from fastapi.testclient import TestClient

        from backglass.web.app import create_app

        return TestClient(
            create_app(settings, on_open=lambda: calls.append(1)),
            base_url="http://127.0.0.1:8765",
        )

    def test_a_page_load_with_a_missing_plan_triggers_the_net(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(catchup, "hole_exists", lambda *_a, **_k: True)
        calls: list[int] = []
        assert self._client(settings, calls).get("/").status_code == 200
        assert calls == [1]

    def test_a_page_load_with_nothing_missing_triggers_nothing(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(catchup, "hole_exists", lambda *_a, **_k: False)
        calls: list[int] = []
        assert self._client(settings, calls).get("/").status_code == 200
        assert calls == []

    def test_an_htmx_fragment_is_not_an_open(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A swap replaces a panel. The owner opened the app once, not once per click."""
        monkeypatch.setattr(catchup, "hole_exists", lambda *_a, **_k: True)
        calls: list[int] = []
        client = self._client(settings, calls)
        assert client.get("/", headers={"hx-request": "true"}).status_code == 200
        assert calls == []

    def test_a_default_app_never_starts_background_work(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every other dashboard test builds `create_app(settings)`. None of them may
        launch a planner, so the absence of a trigger has to be the default."""
        from fastapi.testclient import TestClient

        from backglass.web.app import create_app

        def boom(*_a: object, **_k: object) -> bool:
            raise AssertionError("an app with no trigger asked about holes")

        monkeypatch.setattr(catchup, "hole_exists", boom)
        client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")
        assert client.get("/").status_code == 200

    def test_a_failing_trigger_still_serves_the_page(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rule 5. The catch-up is a net under the page, never a condition of it."""
        monkeypatch.setattr(catchup, "hole_exists", lambda *_a, **_k: True)

        def boom() -> None:
            raise RuntimeError("no planner today")

        from fastapi.testclient import TestClient

        from backglass.web.app import create_app

        client = TestClient(create_app(settings, on_open=boom), base_url="http://127.0.0.1:8765")
        assert client.get("/").status_code == 200


class TestTheEveningPassRefusesTheMorning:
    """`shutdown` decides what did not get done and rolls it into tomorrow. Fired at 09:30
    by the stale zone, it made that judgement about a day with twelve hours left in it."""

    def _run(  # type: ignore[no-untyped-def]
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        now: datetime,
        *args: str,
    ):
        from typer.testing import CliRunner

        import backglass.__main__ as cli
        from backglass.plan import timezones

        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        monkeypatch.setattr(timezones, "local_now", lambda _s: now)
        return CliRunner().invoke(cli.app, ["shutdown", *args])

    def _closed(self, monkeypatch: pytest.MonkeyPatch) -> list[date]:
        from backglass.plan import rollover

        closed: list[date] = []

        def record(_conn: object, _s: object, day: date, **_k: object):  # type: ignore[no-untyped-def]
            closed.append(day)
            return rollover.CloseReport(day=day)

        monkeypatch.setattr(rollover, "close_day", record)
        return closed

    def test_it_refuses_while_the_window_is_open(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        closed = self._closed(monkeypatch)
        result = self._run(settings, monkeypatch, _at(9, 30))
        assert result.exit_code == 0, result.output
        assert "not closing the day" in result.output
        assert closed == []

    def test_it_closes_the_day_once_the_window_has_ended(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """18:00 exactly, because that is when the job fires — `>` would refuse the one
        run that is on time."""
        closed = self._closed(monkeypatch)
        result = self._run(settings, monkeypatch, _at(18, 0))
        assert result.exit_code == 0, result.output
        assert closed == [DAY]

    def test_force_closes_it_anyway(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        closed = self._closed(monkeypatch)
        self._run(settings, monkeypatch, _at(9, 30), "--force")
        assert closed == [DAY]

    def test_a_past_day_is_never_premature(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Closing a day that is genuinely over is the ordinary catch-up case."""
        closed = self._closed(monkeypatch)
        yesterday = DAY - timedelta(days=1)
        self._run(settings, monkeypatch, _at(9, 30), "--date", yesterday.isoformat())
        assert closed == [yesterday]

    def test_the_hour_is_the_window_the_day_is_governed_by(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A weekend runs on `weekend_window`, so the pass belongs after 18:00 that day
        and not after the weekday 22:00 — the same two-copies failure `schedule.render`
        exists to end, one command over."""
        weekend = settings.model_copy(
            update={
                "working_window": "10:00-22:00",
                "weekend_window": "10:00-18:00",
                "working_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            }
        )
        saturday = date(2026, 8, 22)
        assert saturday.weekday() == 5
        closed = self._closed(monkeypatch)
        at = datetime(2026, 8, 22, 19, 0, tzinfo=PHOENIX)
        self._run(weekend, monkeypatch, at, "--date", saturday.isoformat())
        assert closed == [saturday], "19:00 is after the weekend window's 18:00"


class TestUnchangedContract:
    def test_the_scheduled_job_still_regenerates(self) -> None:
        """Pinned by tests/test_schedule.py for a reason: the 05:45 run is built on the
        overnight batch collect and must be able to replace a pre-dawn login's plan. The
        fix for a deferred firing is the clamped window, never skipping the run."""
        from pathlib import Path

        from backglass import schedule

        plan = schedule.render(Path("/r"), "/u", Path("/h"))["com.backglass.plan.plist"]
        assert "<string>--if-missing</string>" not in plan


class TestEstimates:
    def test_the_backlog_follows_a_changed_defaults_table(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """256 open commitments sat on type_default:45 from the old four-pattern table.
        A NULL-only backfill would have left every one of them there forever."""
        from backglass.plan import estimates

        cid = _commitment(conn, "submit the volunteer application", minutes=45)
        conn.execute(
            "UPDATE commitment SET estimate_source = 'type_default' WHERE id = ?", (cid,)
        )
        estimates.backfill(conn, settings)
        row = conn.execute(
            "SELECT estimated_minutes, estimate_source FROM commitment WHERE id = ?", (cid,)
        ).fetchone()
        assert row["estimated_minutes"] == 30  # 'form', not 'unknown'
        assert row["estimate_source"] == "type_default"

    def test_a_manual_estimate_is_never_re_derived(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The stickiness §1.3 promises. A number the owner chose survives every run."""
        from backglass.plan import estimates

        cid = _commitment(conn, "submit the volunteer application", minutes=5)
        estimates.backfill(conn, settings)
        row = conn.execute(
            "SELECT estimated_minutes FROM commitment WHERE id = ?", (cid,)
        ).fetchone()
        assert row["estimated_minutes"] == 5

    def test_a_second_backfill_writes_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Rule 3, applied to a function that now updates as well as fills."""
        from backglass.plan import estimates

        cid = _commitment(conn, "call the volunteer coordinator")
        conn.execute(
            "UPDATE commitment SET estimate_source = 'type_default' WHERE id = ?", (cid,)
        )
        assert estimates.backfill(conn, settings) == 1
        assert estimates.backfill(conn, settings) == 0

    def test_the_measured_verbs_all_classify(self) -> None:
        """Counted from the owner's own open backlog on 2026-08-15, where 273 of 287
        commitments fell through to `unknown` and therefore to 45 minutes."""
        from backglass.plan import estimates

        for what, kind in [
            ("send the transcript to admissions", "message"),
            ("reply to the housing office", "message"),
            ("complete the AES agreement form", "form"),
            ("submit volunteer application on HOV website", "form"),
            ("apply for the summer research programme", "form"),
            ("accept Academic Excellence Scholarship award", "form"),
            ("register for BIO 282", "form"),
            ("call the volunteer coordinator", "call"),
            ("pick up the parking permit", "errand"),
            ("pay the housing deposit", "errand"),
            ("log the six McKenna lunch conversations", "log"),
        ]:
            assert estimates.classify(what) == kind, what

    def test_unknown_still_falls_back_to_45(self, settings: Settings) -> None:
        """The pessimistic default stays for anything genuinely unrecognised."""
        from backglass.plan import estimates

        assert estimates.classify("xyzzy the frobnicator") == "unknown"
        assert estimates.defaults(settings)["unknown"] == 45


class TestTheDayIsOver:
    """A zero window has three causes now, and they are not the same sentence."""

    def test_a_run_past_the_window_says_the_day_is_over(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The first thing the clamp did on the live ledger was report a Saturday the
        owner works as "not a working day", because `no_window` was derived from
        `window_minutes == 0` and the clamp had just made that ambiguous."""
        proposal = planner.propose(conn, settings, DAY, events=[], now=_at(23, 30))
        assert not proposal.capacity.no_window
        assert proposal.capacity.window_closed
        notes = " ".join(proposal.notes)
        assert "not a working day" not in notes
        assert "closed" in notes

    def test_a_genuine_non_working_day_still_says_so(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        weekday_only = settings.model_copy(update={"working_days": ["mon"], "weekend_window": ""})
        proposal = planner.propose(conn, weekday_only, DAY, events=[], now=_at(11, 0))
        assert proposal.capacity.no_window
        assert not proposal.capacity.window_closed
        assert "not a working day" in " ".join(proposal.notes)

    def test_an_unclamped_zero_window_is_unchanged(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """No `now`, no clamp, and the flag stays false — every existing caller keeps
        the two-state behaviour it was written against."""
        weekday_only = settings.model_copy(update={"working_days": ["mon"], "weekend_window": ""})
        cap = capacity_mod.compute(conn, weekday_only, DAY, events=[])
        assert cap.no_window
        assert not cap.window_closed


class TestRetrievalCatchesUp:
    """`search index` was manual-only — nothing in sync.py, nothing in any launchd
    template, ever called it. The backlog grew 8 → 36 in a week with nobody doing
    anything wrong, and the only thing that noticed was a `state` field nobody runs."""

    def test_the_backlog_is_indexed_on_every_sync(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backglass import search

        monkeypatch.setattr(search, "index", lambda *_a, **_k: 7)
        filled = catchup.run(conn, settings, now=_at(4, 0))
        assert [(f.surface, f.detail) for f in filled] == [
            ("retrieval", "7 document(s) indexed")
        ]

    def test_it_runs_even_before_the_morning_hours(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unlike the plan and the brief, indexing has no hour it is owed at."""
        from backglass import search

        calls: list[int] = []
        monkeypatch.setattr(search, "index", lambda *_a, **_k: calls.append(1) or 0)
        catchup.run(conn, settings, now=_at(3, 0))
        assert calls

    def test_an_empty_backlog_says_nothing(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backglass import search

        monkeypatch.setattr(search, "index", lambda *_a, **_k: 0)
        assert catchup.run(conn, settings, now=_at(4, 0)) == []

    def test_a_dead_embedding_endpoint_does_not_take_the_sync_with_it(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Retrieval is additive by CLAUDE.md's ruling. A sync that fails because ollama
        is not running would be the additive layer becoming load-bearing."""
        from backglass import search

        def boom(*_a: object, **_k: object) -> int:
            raise OSError("connection refused")

        monkeypatch.setattr(search, "index", boom)
        assert catchup.run(conn, settings, now=_at(4, 0)) == []


class TestHeartbeatReadsTheSetting:
    """The drift introduced on 2026-08-15: `plan_at` became a setting and the launchd
    template renders from it, while heartbeat kept its own `time(5, 45)`."""

    def test_moving_the_plan_hour_moves_the_alarm(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass import heartbeat

        late = settings.model_copy(update={"plan_at": "11:00"})
        # 10:00 is after the old hardcoded 05:45 + grace and before 11:00 + grace.
        beat = heartbeat.read(conn, late, DAY, _at(10, 0))
        assert not beat.plan_due, "alarmed against the old hardcoded hour"
        assert heartbeat.read(conn, late, DAY, _at(11, 45)).plan_due

    def test_the_default_hour_still_behaves(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass import heartbeat

        assert not heartbeat.read(conn, settings, DAY, _at(5, 50)).plan_due
        assert heartbeat.read(conn, settings, DAY, _at(6, 30)).plan_due


class TestTheWeeklyBriefCountsAsTodaysBrief:
    """docs/05 W1: the Monday brief *replaces* the daily one, and Friday's retro does the
    same. The net looked for `kind = 'daily'` and so never saw them."""

    def _monday(self) -> date:
        return date(2026, 8, 17)

    def test_a_monday_brief_satisfies_the_net(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        monday = self._monday()
        conn.execute(
            "INSERT INTO brief (user_id, generated_for_date, kind, content_md,"
            " items_json, word_count) VALUES (1, ?, 'monday', 'x', '[]', 10)",
            (monday.isoformat(),),
        )
        assert not catchup.brief_is_missing(conn, monday)

    def test_a_friday_retro_does_too(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        friday = date(2026, 8, 14)
        conn.execute(
            "INSERT INTO brief (user_id, generated_for_date, kind, content_md,"
            " items_json, word_count) VALUES (1, ?, 'friday', 'x', '[]', 10)",
            (friday.isoformat(),),
        )
        assert not catchup.brief_is_missing(conn, friday)

    def test_it_does_not_regenerate_on_every_sync(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The measured symptom: 13 regenerations in one Monday, one per half-hour,
        each overwriting the last — a rule 3 violation that cost no rows and so went
        unnoticed until something checked.

        The day gets a live plan too, so the plan half of the net is satisfied and the
        run's emptiness speaks only about the brief."""
        monday = self._monday()
        conn.execute(
            "INSERT INTO day_plan (user_id, local_date, tz, capacity_minutes,"
            " generated_at) VALUES (1, ?, 'America/Phoenix', 240, ?)",
            (monday.isoformat(), f"{monday.isoformat()}T05:45:00-07:00"),
        )
        conn.execute(
            "INSERT INTO brief (user_id, generated_for_date, kind, content_md,"
            " items_json, word_count) VALUES (1, ?, 'monday', 'x', '[]', 10)",
            (monday.isoformat(),),
        )
        at_nine = datetime(monday.year, monday.month, monday.day, 9, 0, tzinfo=PHOENIX)
        assert [f.surface for f in catchup.run(conn, settings, now=at_nine)] == []
