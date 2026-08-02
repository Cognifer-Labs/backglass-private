"""B6: `backglass schedule install`. launchd/templates/*.plist.tmpl -> real files.

`render()` reads the actual shipped templates (no override for the template
directory), so most of these exercise the real seven files with fake substitution
values — the fastest way to catch a template that silently doesn't fill in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backglass import schedule


def test_every_template_renders_with_no_placeholder_left() -> None:
    rendered = schedule.render(Path("/fake/repo/backglass"), "/fake/bin/uv", Path("/fake/home"))
    assert len(rendered) == 7
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


def test_every_rendered_label_is_com_backglass() -> None:
    rendered = schedule.render(Path("/r"), "/u", Path("/h"))
    for filename in rendered:
        assert filename.startswith("com.backglass.")


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

    assert len(rendered) == 7
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
    assert len(loads) == 7
    assert all(cmd[:2] == ["launchctl", "load"] for cmd in loads)
