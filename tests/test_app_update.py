"""Keeping /Applications level with the checkout, on a timer.

The gap: the app freezes its Python, templates and CSS at build time, so every merge
leaves the installed copy behind until somebody rebuilds by hand. On 2026-08-16 the
installed app was running the previous week's planner and `state` said `matches_source:
True`, because it had never hashed the frozen Python. `state` can see it now; this is the
thing that acts on it.

Nothing here shells out. `run` takes its runner, so every test drives the decision and the
order of operations without a nine-minute build or a write to /Applications.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backglass import appupdate


def _bundle(root: Path, *, manifest: str = "") -> Path:
    """A minimal .app: enough structure for the checks under test to read."""
    app = root / "Backglass.app"
    sidecar = app / "Contents/Resources/sidecar/backglass-server"
    sidecar.mkdir(parents=True, exist_ok=True)
    (sidecar / appupdate.state_mod.PYTHON_MANIFEST).write_text(manifest)
    return app


class TestTheDecision:
    def test_no_installed_app_is_not_a_failure(self, tmp_path: Path) -> None:
        """Installing the first copy decides where the app lives — a human act, and not
        something an hourly job should do behind the owner's back."""
        decision = appupdate.decide(tmp_path / "absent.app")
        assert not decision.build
        assert "no app installed" in decision.reason

    def test_a_bundle_predating_the_manifest_is_rebuilt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`state` reports that case as unknown rather than current, and unknown is
        exactly the state a rebuild resolves."""
        app = _bundle(tmp_path)
        (app / "Contents/Resources/sidecar/backglass-server"
         / appupdate.state_mod.PYTHON_MANIFEST).unlink()
        monkeypatch.setattr(appupdate, "working_tree_is_clean", lambda *_a: (True, "clean"))
        decision = appupdate.decide(app)
        assert decision.build
        assert "predates the manifest" in decision.reason

    def test_a_dirty_tree_is_refused_and_says_why(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An app built from uncommitted work is one nobody can check out, diff, or go
        back to. The refusal names the remedy, because this runs unattended."""
        app = _bundle(tmp_path, manifest="deadbeef  backglass/__main__.py\n")
        monkeypatch.setattr(
            appupdate, "working_tree_is_clean", lambda *_a: (False, "3 uncommitted change(s)")
        )
        decision = appupdate.decide(app)
        assert not decision.build
        assert "uncommitted" in decision.reason
        assert "commit them" in decision.reason

    def test_a_current_bundle_costs_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The common case, and the one that must not build: a hash comparison, then
        nothing. A rebuild is nine minutes of CPU and must never run on a hunch."""
        app = _bundle(tmp_path)
        monkeypatch.setattr(
            appupdate, "installed_is_current", lambda *_a: (True, (), None)
        )
        decision = appupdate.decide(app)
        assert not decision.build
        assert "matches the checkout" in decision.reason


class TestWhatItDoes:
    def _runner(self, calls: list[list[str]], *, fail: str | None = None):  # type: ignore[no-untyped-def]
        def run(command: list[str], _cwd: Path) -> int:
            calls.append(command)
            return 1 if fail and any(fail in part for part in command) else 0

        return run

    def test_check_only_never_builds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(
            appupdate, "decide", lambda *_a, **_k: appupdate.Decision(True, "stale")
        )
        result = appupdate.run(check_only=True, runner=self._runner(calls))
        assert calls == []
        assert not result.built

    def test_it_defers_while_the_owner_has_the_app_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An app that quits itself under the reader is a worse surprise than a day-old
        one — and this one dies with its window anyway, so the wait is short."""
        calls: list[list[str]] = []
        monkeypatch.setattr(
            appupdate, "decide", lambda *_a, **_k: appupdate.Decision(True, "stale")
        )
        monkeypatch.setattr(appupdate, "app_is_running", lambda *_a: True)
        result = appupdate.run(runner=self._runner(calls))
        assert calls == []
        assert not result.built
        assert "deferred" in result.notes[0]
        assert result.exit_code == 0, "a deliberate skip is not a failure"

    def test_now_quits_installs_and_relaunches_in_that_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        app = _bundle(tmp_path)
        bundle = _bundle(tmp_path / "built")
        monkeypatch.setattr(
            appupdate, "decide", lambda *_a, **_k: appupdate.Decision(True, "stale")
        )
        monkeypatch.setattr(appupdate, "app_is_running", lambda *_a: True)
        monkeypatch.setattr(appupdate, "BUNDLE", bundle)
        monkeypatch.setattr(appupdate, "BACKUP_DIR", tmp_path / "backups")
        monkeypatch.setattr(appupdate, "LOCK_PATH", tmp_path / "build.lock")

        result = appupdate.run(now=True, app=app, runner=self._runner(calls))

        assert result.built and result.installed
        flat = [" ".join(c) for c in calls]
        order = [
            next(i for i, c in enumerate(flat) if "build-sidecar.sh" in c),
            next(i for i, c in enumerate(flat) if "to quit" in c),
            next(i for i, c in enumerate(flat) if c.startswith("-c ditto") and "backups" in c),
            next(i for i, c in enumerate(flat) if "rm -rf" in c),
            next(i for i, c in enumerate(flat) if "open -a" in c),
        ]
        assert order == sorted(order), flat

    def test_an_unsigned_build_never_touches_the_installed_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The order is the safety: verify the new bundle before removing the old one, so
        a failure anywhere leaves something launchable in /Applications."""
        calls: list[list[str]] = []
        app = _bundle(tmp_path)
        monkeypatch.setattr(
            appupdate, "decide", lambda *_a, **_k: appupdate.Decision(True, "stale")
        )
        monkeypatch.setattr(appupdate, "app_is_running", lambda *_a: False)
        monkeypatch.setattr(appupdate, "BUNDLE", _bundle(tmp_path / "built"))
        monkeypatch.setattr(appupdate, "LOCK_PATH", tmp_path / "build.lock")

        result = appupdate.run(app=app, runner=self._runner(calls, fail="codesign"))

        assert result.built and not result.installed
        assert any("codesign" in n for n in result.notes)
        assert not any("rm -rf" in " ".join(c) for c in calls)
        assert result.exit_code == 1, "an attempted install that failed is a failure"

    def test_a_second_rebuild_skips_while_the_first_holds_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two nine-minute builds writing the same staging directory is worse than one."""
        import fcntl

        calls: list[list[str]] = []
        lock = tmp_path / "build.lock"
        monkeypatch.setattr(appupdate, "LOCK_PATH", lock)
        monkeypatch.setattr(
            appupdate, "decide", lambda *_a, **_k: appupdate.Decision(True, "stale")
        )
        monkeypatch.setattr(appupdate, "app_is_running", lambda *_a: False)

        lock.parent.mkdir(parents=True, exist_ok=True)
        held = lock.open("a+")
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = appupdate.run(app=_bundle(tmp_path), runner=self._runner(calls))
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
            held.close()

        assert not result.built
        assert "already running" in result.notes[0]
        assert result.exit_code == 0


class TestTheJob:
    def test_it_is_an_interval_job_not_a_calendar_one(self) -> None:
        """The lesson of 2026-08-17, pinned where it applies: this machine's calendar
        agent evaluates fire times in the zone the Mac booted in, and a job registered
        seconds ago inherits the stale zone. Seconds are the same in every zone."""
        import plistlib

        from backglass import schedule

        rendered = schedule.render(Path("/r"), "/u", Path("/h"))
        parsed = plistlib.loads(rendered["com.backglass.app-update.plist"].encode())
        assert "StartCalendarInterval" not in parsed
        assert parsed["StartInterval"] == 3600
        assert parsed["ProgramArguments"][-1] == "app-update"
