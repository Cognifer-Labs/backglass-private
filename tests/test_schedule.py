"""B6: `backglass schedule install`. launchd/templates/*.plist.tmpl -> real files.

`render()` reads the actual shipped templates (no override for the template
directory), so most of these exercise the real nine files with fake substitution
values — the fastest way to catch a template that silently doesn't fill in.

The last class covers the other thing called "schedule": the Schedule *page*, and
specifically the one merge point where its two readers stop describing the same event
twice (`web/routes/schedule._collapse`).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from backglass import schedule
from backglass.config import Settings
from backglass.plan import capacity
from backglass.web.routes import schedule as schedule_page


def test_every_template_renders_with_no_placeholder_left() -> None:
    rendered = schedule.render(Path("/fake/repo/backglass"), "/fake/bin/uv", Path("/fake/home"))
    assert len(rendered) == 9
    for filename, text in rendered.items():
        assert filename.endswith(".plist")
        assert not filename.endswith(".plist.tmpl")
        assert "{{" not in text
        assert "}}" not in text


def test_resolves_to_the_given_repo_and_uv_paths() -> None:
    rendered = schedule.render(Path("/fake/repo/backglass"), "/fake/bin/uv", Path("/fake/home"))
    sync = rendered["com.backglass.sync.plist"]
    assert "<string>/fake/repo/backglass</string>" in sync
    assert "<string>/fake/bin/uv</string>" in sync
    assert "<string>com.backglass.sync</string>" in sync
    assert "/fake/repo/backglass/data/sync.log" in sync


def test_every_rendered_plist_actually_parses_as_a_plist() -> None:
    # Rendering-with-no-placeholders is not the same as valid XML: the first draft of
    # the catch-up template explained the `if-missing` flag in a comment and XML forbids
    # a double hyphen inside one, so launchd would have rejected the file at load with
    # nothing but a syslog line to show for it. Parse, don't eyeball.
    import plistlib

    rendered = schedule.render(Path("/fake/repo"), "/fake/uv", Path("/fake/home"))
    for filename, text in rendered.items():
        parsed = plistlib.loads(text.encode())
        assert parsed["Label"] == filename.removesuffix(".plist"), filename
        assert parsed["ProgramArguments"][0] == "/fake/uv", filename


def test_only_the_catchup_job_runs_at_load() -> None:
    # RunAtLoad fires at login — that is the whole point of the catch-up job, and it is
    # only safe because its command carries --if-missing. Every other job must stay
    # <false/>: a `schedule install` that fired sync, brief --send and shutdown on the
    # spot would email a brief and close the day the moment you logged in.
    rendered = schedule.render(Path("/r"), "/u", Path("/h"))
    catchup = rendered["com.backglass.plan-catchup.plist"]
    assert "<key>RunAtLoad</key><true/>" in catchup
    assert "<string>--if-missing</string>" in catchup
    assert "<key>StartCalendarInterval</key>" not in catchup
    for filename, text in rendered.items():
        if filename != "com.backglass.plan-catchup.plist":
            assert "<key>RunAtLoad</key><true/>" not in text, filename


def test_the_scheduled_plan_job_always_regenerates() -> None:
    # The 05:45 run must NOT carry --if-missing: it is the run whose plan is built on the
    # overnight batch collect, and it would be skipped by a plan the catch-up wrote at a
    # pre-dawn login.
    plan = schedule.render(Path("/r"), "/u", Path("/h"))["com.backglass.plan.plist"]
    assert "<string>--if-missing</string>" not in plan
    assert "<key>Hour</key><integer>5</integer>" in plan


def test_the_backup_job_runs_daily_at_two() -> None:
    # Audit #23: the ledger is the only copy of the record, so this job existing on the
    # right cadence is the whole safety net.
    import plistlib

    rendered = schedule.render(Path("/r"), "/u", Path("/h"))
    backup = plistlib.loads(rendered["com.backglass.backup.plist"].encode())
    assert backup["ProgramArguments"][-1] == "backup"
    assert backup["StartCalendarInterval"] == {"Hour": 2, "Minute": 0}


def test_every_rendered_label_is_com_backglass() -> None:
    rendered = schedule.render(Path("/r"), "/u", Path("/h"))
    for filename in rendered:
        assert filename.startswith("com.backglass.")


class TestTheJobsFireWhenTheSettingsSayTheyDo:
    """A time the owner configures once must not need remembering in a second place.

    The shutdown template carried 18:00 from the 09:00–18:00 default. The owner's window
    is 10:00–22:00, so docs/04 §1.8's "second, much smaller pass at the end of the working
    window" ran with four hours of that window left — and, since the gym routine starts
    at 17:30, ran it at an empty desk. Nothing on either side said the two disagreed,
    which is the property these tests exist to make impossible.
    """

    def _job(self, name: str, **overrides: object) -> dict[str, object]:
        import plistlib

        settings = Settings(**overrides)  # type: ignore[arg-type]
        rendered = schedule.render(Path("/r"), "/u", Path("/h"), settings)
        return plistlib.loads(rendered[name].encode())

    def test_the_evening_pass_runs_when_the_working_day_ends(self) -> None:
        job = self._job("com.backglass.shutdown.plist", working_window="10:00-22:00")
        assert job["StartCalendarInterval"] == {"Hour": 22, "Minute": 0}

    def test_moving_the_window_moves_the_evening_pass_with_it(self) -> None:
        job = self._job("com.backglass.shutdown.plist", working_window="09:00-18:30")
        assert job["StartCalendarInterval"] == {"Hour": 18, "Minute": 30}

    def test_the_latest_window_the_config_allows_still_renders_a_real_hour(self) -> None:
        """`shutdown_time` carries no midnight clamp because it cannot need one: the
        `working_window` validator refuses "10:00-24:00" outright. This pins the reason,
        so a later loosening of that validator fails here rather than silently rendering
        `<integer>24</integer>` into a plist launchd will not load."""
        assert schedule.shutdown_time(Settings(working_window="10:00-23:59")) == (23, 59)
        with pytest.raises(ValueError):
            Settings(working_window="10:00-24:00")

    def test_the_brief_job_fires_at_brief_at(self) -> None:
        job = self._job("com.backglass.brief.plist", brief_at="05:30")
        assert job["StartCalendarInterval"] == {"Hour": 5, "Minute": 30}

    def test_the_planner_still_runs_before_the_brief_it_feeds(self) -> None:
        """Not derived from settings, so this is the assertion that keeps the pair sane:
        a brief generated before the plan it reports is a brief about yesterday."""
        import plistlib

        settings = Settings(brief_at="06:00")
        rendered = schedule.render(Path("/r"), "/u", Path("/h"), settings)
        plan = plistlib.loads(rendered["com.backglass.plan.plist"].encode())
        brief = plistlib.loads(rendered["com.backglass.brief.plist"].encode())
        plan_at = (plan["StartCalendarInterval"]["Hour"], plan["StartCalendarInterval"]["Minute"])
        brief_at = (brief["StartCalendarInterval"]["Hour"], brief["StartCalendarInterval"]["Minute"])
        assert plan_at < brief_at


def test_raises_when_the_template_directory_is_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(schedule, "TEMPLATE_DIR", tmp_path)
    with pytest.raises(schedule.ScheduleError, match="no \\*.plist.tmpl"):
        schedule.render(Path("/r"), "/u", Path("/h"))


def test_find_uv_raises_a_clear_error_when_not_on_path(monkeypatch) -> None:
    monkeypatch.setattr(schedule.shutil, "which", lambda name: None)
    with pytest.raises(schedule.ScheduleError, match="uv not found"):
        schedule.find_uv()


def test_find_uv_returns_whatever_which_finds(monkeypatch) -> None:
    monkeypatch.setattr(schedule.shutil, "which", lambda name: "/opt/homebrew/bin/uv")
    assert schedule.find_uv() == "/opt/homebrew/bin/uv"


def test_dry_run_renders_but_writes_and_loads_nothing(monkeypatch) -> None:
    writes: list[Path] = []
    loads: list[list[str]] = []
    monkeypatch.setattr(Path, "write_text", lambda self, *a, **k: writes.append(self))
    monkeypatch.setattr(
        schedule.subprocess, "run", lambda cmd, **k: loads.append(cmd)
    )

    rendered = schedule.install(dry_run=True, uv_bin="/fake/uv")

    assert len(rendered) == 9
    assert writes == []
    assert loads == []


def test_real_run_writes_and_loads_each_job(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(schedule, "LAUNCH_AGENTS_DIR", tmp_path / "LaunchAgents")
    loads: list[list[str]] = []
    monkeypatch.setattr(
        schedule.subprocess, "run", lambda cmd, **k: loads.append(cmd)
    )

    rendered = schedule.install(dry_run=False, uv_bin="/fake/uv")

    written_dir = tmp_path / "LaunchAgents"
    for filename in rendered:
        assert (written_dir / filename).read_text() == rendered[filename]
    assert len(loads) == 2 * len(rendered)  # unload then load, per job
    assert all(cmd[:2] in (["launchctl", "unload"], ["launchctl", "load"]) for cmd in loads)


def test_every_job_is_unloaded_before_it_is_loaded(monkeypatch, tmp_path) -> None:
    """Otherwise installing a changed schedule changes only the file.

    `launchctl load` does not reload a job that is already loaded: it fails with
    "Load failed: 5: Input/output error" and leaves the old definition registered. Every
    machine that would run this command has already installed these jobs, so moving the
    shutdown hour to 22:00 wrote 22 to disk while `launchctl print` kept reporting 18 —
    and `installed com.backglass.shutdown.plist` printed either way.
    """
    monkeypatch.setattr(schedule, "LAUNCH_AGENTS_DIR", tmp_path / "LaunchAgents")
    calls: list[list[str]] = []
    monkeypatch.setattr(schedule.subprocess, "run", lambda cmd, **k: calls.append(cmd))

    schedule.install(dry_run=False, uv_bin="/fake/uv")

    for job in {cmd[2] for cmd in calls}:
        actions = [cmd[1] for cmd in calls if cmd[2] == job]
        assert actions == ["unload", "load"], f"{job} was not reloaded, only loaded"


def test_no_api_key_skips_the_batch_jobs() -> None:
    """A job whose every fire can only exit 2 is not a schedule, it is an error log."""
    rendered = schedule.install(dry_run=True, uv_bin="/fake/uv", batch_lane=False)
    assert rendered  # the other six still ship
    assert not any(f.startswith(schedule.BATCH_PREFIX) for f in rendered)
    assert "com.backglass.sync.plist" in rendered


def test_a_key_keeps_the_batch_jobs() -> None:
    rendered = schedule.install(dry_run=True, uv_bin="/fake/uv", batch_lane=True)
    assert "com.backglass.batch-submit.plist" in rendered
    assert "com.backglass.batch-collect.plist" in rendered


def test_losing_the_key_unloads_previously_installed_batch_jobs(
    monkeypatch, tmp_path
) -> None:
    """The machine this fixes: batch jobs installed back when they seemed harmless,
    now failing every night. A re-run of `schedule install` must take them out, not
    merely stop adding them."""
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    stale = agents / "com.backglass.batch-submit.plist"
    stale.write_text("<plist/>")
    calls: list[list[str]] = []
    monkeypatch.setattr(schedule, "LAUNCH_AGENTS_DIR", agents)
    monkeypatch.setattr(schedule.subprocess, "run", lambda cmd, **k: calls.append(cmd))

    rendered = schedule.install(dry_run=False, uv_bin="/fake/uv", batch_lane=False)

    assert not stale.exists()
    assert ["launchctl", "unload", str(stale)] in calls
    loaded = [cmd for cmd in calls if cmd[1] == "load"]
    assert loaded
    assert not any(schedule.BATCH_PREFIX in cmd[2] for cmd in loaded)
    assert not any(f.startswith(schedule.BATCH_PREFIX) for f in rendered)


# ── the Schedule page: one event, one entry ───────────────────────────────

DAY = date(2026, 8, 20)
TZ = "America/Phoenix"


def _block(starts_at: str, ends_at: str, title: str, kind: str) -> dict[str, Any]:
    """One `dashboard_today.sql` row, cut down to the columns the timeline reads."""
    return {
        "starts_at": starts_at,
        "ends_at": ends_at,
        "title": title,
        "kind": kind,
        "outcome": "",
    }


def _view(
    fixed: list[capacity.FixedEvent], blocks: list[dict[str, Any]]
) -> schedule_page.DayView:
    return schedule_page.DayView(day=DAY, tz=TZ, blocks=blocks, fixed=fixed)


class TestTheTimelineDrawsEachEventOnce:
    """The page reads the ledger and the persisted plan, and both hold the same event.

    Found on the owner's real store: 2026-08-20 drew 16 entries for 9 things, the copies
    stacked pixel-identically because only two lanes exist, so the canvas asserted a
    triple-booked day directly under the capacity sentence saying the day fits.
    """

    def _real_shaped_day(self) -> schedule_page.DayView:
        """All three duplication shapes at once, in the owner's real spellings.

        HON 171 is the planner re-projection: `plan/planner` persists a `kind='fixed'`
        block for every event in `cap.fixed`, so the class arrives once from
        `dashboard_today.sql` and once from `capacity.fixed_events`.

        PSY 101 is the ASU/Apple double import — the same lecture, stored as a local
        offset by one connector and as UTC by the other. Both go through
        `capacity._aware`, which resolves them to the same Phoenix wall clock, and that
        is why identity can be a start minute.

        Dinner is the shape that must SURVIVE: an engagement-derived fixed block
        ("dinner at seven") reaches the plan through `capacity.engagement_events` and has
        no calendar `source_item` behind it, so it exists only as a `kind='fixed'` block.
        Filtering fixed blocks out instead of collapsing on identity would delete it.
        """
        return _view(
            fixed=[
                capacity.FixedEvent(
                    starts_at=capacity._aware("2026-08-20T10:30:00-07:00", TZ),
                    ends_at=capacity._aware("2026-08-20T11:45:00-07:00", TZ),
                    title="HON 171",
                ),
                capacity.FixedEvent(
                    starts_at=capacity._aware("2026-08-20T12:00:00-07:00", TZ),
                    ends_at=capacity._aware("2026-08-20T13:15:00-07:00", TZ),
                    title="PSY 101",
                ),
                capacity.FixedEvent(
                    starts_at=capacity._aware("2026-08-20T19:00:00.000Z", TZ),
                    ends_at=capacity._aware("2026-08-20T20:15:00.000Z", TZ),
                    title="PSY 101",
                ),
            ],
            blocks=[
                _block(
                    "2026-08-20T10:30:00-07:00", "2026-08-20T11:45:00-07:00", "HON 171", "fixed"
                ),
                _block(
                    "2026-08-20T19:00:00-07:00", "2026-08-20T20:30:00-07:00",
                    "Dinner — Postino", "fixed",
                ),
                _block(
                    "2026-08-20T13:25:00-07:00", "2026-08-20T13:55:00-07:00",
                    "Loan application", "work",
                ),
            ],
        )

    def test_every_duplication_shape_collapses_and_the_engagement_block_survives(self) -> None:
        entries = schedule_page.timeline(self._real_shaped_day(), today=DAY).entries

        assert [(e.start_label, e.title) for e in entries] == [
            ("10:30am", "HON 171"),
            ("12:00pm", "PSY 101"),
            ("1:25pm", "Loan application"),
            ("7:00pm", "Dinner — Postino"),
        ]
        # Nothing was pushed into the overflow lane, because nothing overlaps any more.
        assert {e.lane for e in entries} == {0}

    def test_the_week_grid_collapses_too(self) -> None:
        """Same defect, same fix: the week columns place `_raw_entries` output as well."""
        week = schedule_page.week_timeline([self._real_shaped_day()], today=DAY)
        assert len(week.cols[0].entries) == 4

    def test_the_reader_that_knew_it_was_travel_is_not_overruled(self) -> None:
        """`capacity._distinct` OR-s the flag; the copies reaching this page are worse —
        a plan block cannot carry travel at all, so a first-wins collapse would drop the
        commute hatching from an event the calendar had marked."""
        view = _view(
            fixed=[
                capacity.FixedEvent(
                    starts_at=capacity._aware("2026-08-20T08:00:00-07:00", TZ),
                    ends_at=capacity._aware("2026-08-20T08:40:00-07:00", TZ),
                    title="Drive to campus",
                    travel=True,
                )
            ],
            blocks=[
                _block(
                    "2026-08-20T08:00:00-07:00", "2026-08-20T08:40:00-07:00",
                    "Drive to campus", "fixed",
                )
            ],
        )
        entries = schedule_page.timeline(view, today=DAY).entries
        assert len(entries) == 1
        assert entries[0].travel

    def test_two_genuinely_different_events_at_the_same_minute_both_draw(self) -> None:
        """Identity is all three fields. A double-booked hour is real information."""
        view = _view(
            fixed=[
                capacity.FixedEvent(
                    starts_at=capacity._aware("2026-08-20T09:00:00-07:00", TZ),
                    ends_at=capacity._aware("2026-08-20T10:00:00-07:00", TZ),
                    title="Advising",
                ),
                capacity.FixedEvent(
                    starts_at=capacity._aware("2026-08-20T09:00:00-07:00", TZ),
                    ends_at=capacity._aware("2026-08-20T10:00:00-07:00", TZ),
                    title="Lab safety training",
                ),
            ],
            blocks=[],
        )
        entries = schedule_page.timeline(view, today=DAY).entries
        assert len(entries) == 2
        assert {e.lane for e in entries} == {0, 1}


class TestNoTwoEntriesShareALaneAndAnHour:
    """The evening of 2026-08-09, where five things overlapped and only two lanes existed.

    `_place` decided lane 0 on `start >= lane_ends[0]` and fell through to lane 1 without
    asking whether lane 1 was free, so the shower and the dinner were both drawn on top of
    a two-hour McKenna dinner. The screenshot is the failure: three blocks of unreadable
    text in one column. This is the invariant that makes that unrepresentable.
    """

    def _evening(self) -> schedule_page.DayView:
        return _view(
            fixed=[
                capacity.FixedEvent(
                    starts_at=capacity._aware("2026-08-20T18:00:00-07:00", TZ),
                    ends_at=capacity._aware("2026-08-20T20:00:00-07:00", TZ),
                    title="McKenna Program Welcome Dinner",
                ),
                capacity.FixedEvent(
                    starts_at=capacity._aware("2026-08-20T18:30:00-07:00", TZ),
                    ends_at=capacity._aware("2026-08-20T19:30:00-07:00", TZ),
                    title="McKenna Summer Program kickoff dinner",
                ),
            ],
            blocks=[
                _block(
                    "2026-08-20T17:30:00-07:00", "2026-08-20T18:30:00-07:00", "Gym", "routine"
                ),
                _block(
                    "2026-08-20T18:35:00-07:00", "2026-08-20T19:00:00-07:00", "Shower", "routine"
                ),
                _block(
                    "2026-08-20T19:15:00-07:00", "2026-08-20T20:00:00-07:00", "Dinner", "routine"
                ),
                # Clear of the pile-up, and the proof that lanes are a property of the
                # collision rather than of the day: this one keeps the full width.
                _block(
                    "2026-08-20T21:00:00-07:00", "2026-08-20T21:30:00-07:00",
                    "Log the day", "work",
                ),
            ],
        )

    def test_no_two_entries_in_one_lane_cover_the_same_minute(self) -> None:
        entries = schedule_page.timeline(self._evening(), today=DAY).entries

        by_lane: dict[int, list[schedule_page.Entry]] = {}
        for entry in entries:
            by_lane.setdefault(entry.lane, []).append(entry)
        for lane_entries in by_lane.values():
            spans = sorted((e.top, e.top + e.height) for e in lane_entries)
            for (_, earlier_end), (later_top, _) in zip(spans, spans[1:]):
                assert later_top >= earlier_end

    def test_the_cluster_is_as_wide_as_it_needs_and_no_wider(self) -> None:
        entries = schedule_page.timeline(self._evening(), today=DAY).entries
        widths = {e.title: (e.lane, e.lanes) for e in entries}

        # Five overlapping things, three columns: gym then kickoff dinner share one,
        # shower then dinner share another, the two-hour welcome dinner holds the third.
        assert {lanes for _, lanes in widths.values() if lanes > 1} == {3}
        assert widths["Log the day"] == (0, 1)


def test_dedup_keeps_the_outcome_only_the_plan_block_records() -> None:
    """The mirror of the travel rule, and the one a winner-takes-all merge loses.

    `_raw_entries` appends the calendar copies first and hardcodes `outcome=""`, so the
    plan block — the only reader that knows an event was done or rolled — is always the
    loser on identity. Collapsing to either copy whole drops a fact the other held.
    """
    from backglass.web.routes.schedule import _collapse

    calendar_copy = (540, 60, "CHM 113 (Lab)", "fixed", "", True)
    plan_block = (540, 60, "CHM 113 (Lab)", "fixed", "done", False)

    collapsed = _collapse([calendar_copy, plan_block])

    assert len(collapsed) == 1
    assert collapsed[0][4] == "done", "the outcome survived the collapse"
    assert collapsed[0][5] is True, "and travel is still OR-ed, not overruled"
