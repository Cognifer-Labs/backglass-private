"""`backglass state` — ground truth, with the derivation of every claim.

The tests are about the three properties that make the answer worth trusting, not about
the particular numbers, which are whatever the ledger happens to hold: every claim names
how it was derived, a probe that cannot run says so instead of reading as zero, and one
broken probe does not blank the rest of the answer.
"""

from __future__ import annotations

import json
import sqlite3

from backglass import state as state_mod
from backglass.config import Settings


class TestEveryClaimIsCheckable:
    def test_every_claim_names_its_derivation(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The whole point. A number with no derivation is one a reader has to trust;
        one that names its query is one they can re-run."""
        snapshot = state_mod.collect(conn, settings)
        assert snapshot.sections, "the snapshot is not empty"
        for section, claims in snapshot.sections.items():
            assert claims, f"{section} has no claims"
            for name, claim in claims.items():
                assert claim.how.strip(), f"{section}.{name} does not say how it was derived"

    def test_the_json_is_parseable_and_carries_how(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """`--json` is the surface written for a program to read, so its shape is part
        of the contract rather than a rendering detail."""
        parsed = json.loads(state_mod.as_json(state_mod.collect(conn, settings)))
        for claims in parsed.values():
            for claim in claims.values():
                assert "value" in claim and "how" in claim


class TestUnknownIsAValue:
    def test_a_probe_that_cannot_run_says_so_rather_than_reporting_zero(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch
    ) -> None:
        """The failure this module exists to prevent is a confident answer assembled
        from a missing input. An uninstalled app is a legitimate state, and it must not
        render as "matches source: False" — which would read as *stale*, the opposite
        of the truth."""
        monkeypatch.setattr(state_mod, "INSTALLED_APP", conn_path := _missing_path())
        snapshot = state_mod.collect(conn, settings)
        app = snapshot.sections["deployed"]["app"]
        assert app.unknown == "not installed"
        assert app.value is None
        assert "matches_source" not in snapshot.sections["deployed"], (
            "an absent app must not be reported as a mismatch"
        )
        del conn_path

    def test_an_empty_telemetry_table_is_named_not_silent(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """A fresh ledger has made no model calls. Reporting `{}` with no explanation
        would read as "the pipeline is doing nothing", which is a different claim."""
        snapshot = state_mod.collect(conn, settings)
        calls = snapshot.sections["pipeline"]["model_calls"]
        assert calls.unknown and "fills from the first sync" in calls.unknown


class TestOneBrokenProbeDoesNotBlankTheAnswer:
    def test_a_raising_probe_is_reported_by_name(
        self, conn: sqlite3.Connection, settings: Settings, monkeypatch
    ) -> None:
        """The first version of this module swallowed a KeyError into an anonymous
        lambda, so the report said "a lambda raised" and every other section printed
        happily around the hole. A reader has to know WHICH part of the answer is
        missing, or a partial truth is worse than none."""

        def boom(*_: object, **__: object) -> None:
            raise RuntimeError("the ledger probe fell over")

        monkeypatch.setattr(state_mod, "_ledger", boom)
        snapshot = state_mod.collect(conn, settings)
        assert "ledger" in snapshot.sections["errors"]
        assert "fell over" in (snapshot.sections["errors"]["ledger"].unknown or "")
        # And the rest of the answer survived.
        assert "schema" in snapshot.sections
        assert "knowledge_base" in snapshot.sections


class TestTheDeployedComparison:
    """A version number has to be maintained and can lie; a hash cannot.

    This is the claim that motivated the module. The desktop app freezes templates and
    CSS into its bundle, so the running app can be arbitrarily far behind the checkout
    with nothing on either side saying so — and the only way it was ever noticed was by
    rebuilding and looking at the result.
    """

    def _bundle(self, root, matching: bool):  # type: ignore[no-untyped-def]
        """A fake install: the frozen surfaces copied from the repo, optionally with one
        byte changed to stand for a build that has fallen behind."""
        from backglass.config import REPO_ROOT

        internal = root / "Contents/Resources/sidecar/backglass-server/_internal"
        for relative in state_mod.frozen_surfaces():
            target = internal / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            body = (REPO_ROOT / relative).read_text()
            target.write_text(body if matching else body + "\n/* older build */\n")
        return root

    def test_a_bundle_built_from_this_checkout_matches(
        self, conn: sqlite3.Connection, settings: Settings, tmp_path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(state_mod, "INSTALLED_APP", self._bundle(tmp_path, True))
        deployed = state_mod.collect(conn, settings).sections["deployed"]
        assert deployed["matches_source"].value is True
        assert deployed["stale_surfaces"].value == []

    def test_one_changed_byte_is_enough_to_report_stale(
        self, conn: sqlite3.Connection, settings: Settings, tmp_path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """The failure mode is a bundle that is *almost* current — a rebuild that missed
        one file reads as fine under any check coarser than this."""
        monkeypatch.setattr(state_mod, "INSTALLED_APP", self._bundle(tmp_path, False))
        deployed = state_mod.collect(conn, settings).sections["deployed"]
        assert deployed["matches_source"].value is False
        assert set(deployed["stale_surfaces"].value) == set(state_mod.frozen_surfaces())

    def test_a_stale_template_is_reported_and_not_only_the_stylesheets(
        self, conn: sqlite3.Connection, settings: Settings, tmp_path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """The gap that let the 2026-08-09 timeline fix look deployed when it was not.

        `FROZEN_SURFACES` named two stylesheets. That change also touched two templates,
        equally frozen and equally stale, and `state` said `dashboard.css` alone — so the
        one command CLAUDE.md says to trust before blaming the app under-reported exactly
        the surfaces the app was serving. Here the stylesheets match and only a template
        differs, which is the case the old list could not see at all.
        """
        from backglass.config import REPO_ROOT

        root = self._bundle(tmp_path, True)
        template = (
            root
            / "Contents/Resources/sidecar/backglass-server/_internal"
            / "backglass/web/templates/schedule.html"
        )
        template.write_text((REPO_ROOT / "backglass/web/templates/schedule.html").read_text()
                            + "\n{# older build #}\n")
        monkeypatch.setattr(state_mod, "INSTALLED_APP", root)

        deployed = state_mod.collect(conn, settings).sections["deployed"]
        assert deployed["matches_source"].value is False
        assert deployed["stale_surfaces"].value == ["backglass/web/templates/schedule.html"]

    def test_every_template_on_disk_is_a_surface_that_gets_checked(self) -> None:
        """Globbed, not listed, so a template added later is covered without anyone
        deciding to cover it — the property the hardcoded tuple could not hold."""
        from backglass.config import REPO_ROOT

        surfaces = set(state_mod.frozen_surfaces())
        on_disk = {
            str(p.relative_to(REPO_ROOT))
            for p in (REPO_ROOT / state_mod.FROZEN_TEMPLATE_DIR).glob("*.html")
        }
        assert on_disk and on_disk <= surfaces
        assert set(state_mod.FROZEN_STYLESHEETS) <= surfaces


def _missing_path():  # type: ignore[no-untyped-def]
    from pathlib import Path

    return Path("/nonexistent/Backglass.app")


class TestTheScheduleIsAClaimThatCanBeChecked:
    """The failure this section was written for, on 2026-08-11.

    All four calendar jobs were firing seven and a half hours early: the morning brief
    was written at 17:30 and the day planner ran at 17:17, planning a day that was over
    — which is why it reported a fully booked day with a hundred items overflowing, and
    why that looked like a planner bug. The plists said 06:00 and 05:45, and `launchctl
    print` agreed with them, because the hour was never what was wrong.

    Every existing check read one side or the other and both sides looked right. What
    nobody could see was the distance between them, which is what this reports.
    """

    def _agents(self, root, jobs):  # type: ignore[no-untyped-def]
        """A fake LaunchAgents dir: one plist per job, with its log stamped at the hour
        the job was actually last seen to run."""
        import os
        import plistlib
        from datetime import datetime

        root.mkdir(parents=True, exist_ok=True)
        for label, (scheduled, ran_at) in jobs.items():
            log = root / f"{label}.log"
            log.write_text("ran\n")
            if ran_at is not None:
                when = datetime.now().replace(
                    hour=ran_at[0], minute=ran_at[1], second=0, microsecond=0
                ).timestamp()
                os.utime(log, (when, when))
            body: dict = {"Label": label, "StandardOutPath": str(log)}
            if scheduled is not None:
                body["StartCalendarInterval"] = {
                    "Hour": scheduled[0], "Minute": scheduled[1]
                }
            else:
                body["StartInterval"] = 1800
            (root / f"{label}.plist").write_bytes(plistlib.dumps(body))
        return root

    def test_a_job_that_fires_when_it_says_is_not_reported(
        self, conn: sqlite3.Connection, settings: Settings, tmp_path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        agents = self._agents(
            tmp_path / "LaunchAgents",
            {"com.backglass.brief": ((6, 0), (6, 2))},  # two minutes late is not news
        )
        monkeypatch.setattr(state_mod, "LAUNCH_AGENTS_DIR", agents)
        schedule = state_mod.collect(conn, settings).sections["schedule"]
        assert schedule["drifting"].value == []
        assert "06:00 daily" in schedule["jobs"].value["com.backglass.brief"]

    def test_the_seven_and_a_half_hour_drift_is_reported(
        self, conn: sqlite3.Connection, settings: Settings, tmp_path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """The real numbers off the machine it happened on."""
        agents = self._agents(
            tmp_path / "LaunchAgents",
            {
                "com.backglass.brief": ((6, 0), (17, 30)),
                "com.backglass.plan": ((5, 45), (17, 15)),
            },
        )
        monkeypatch.setattr(state_mod, "LAUNCH_AGENTS_DIR", agents)
        schedule = state_mod.collect(conn, settings).sections["schedule"]
        assert len(schedule["drifting"].value) == 2
        assert any("brief" in line for line in schedule["drifting"].value)

    def test_drift_is_measured_around_the_clock_not_across_it(
        self, conn: sqlite3.Connection, settings: Settings, tmp_path, monkeypatch
    ) -> None:
        """A 00:05 job last seen at 23:50 is fifteen minutes early, not twenty-three
        hours and forty-five minutes late. Measured the naive way, every job scheduled
        near midnight reports as broken forever, and a section that cries wolf nightly
        is one nobody reads on the morning it is right."""
        agents = self._agents(
            tmp_path / "LaunchAgents", {"com.backglass.backup": ((0, 5), (23, 50))}
        )
        monkeypatch.setattr(state_mod, "LAUNCH_AGENTS_DIR", agents)
        schedule = state_mod.collect(conn, settings).sections["schedule"]
        assert schedule["drifting"].value == []

    def test_an_interval_job_cannot_drift(
        self, conn: sqlite3.Connection, settings: Settings, tmp_path, monkeypatch
    ) -> None:
        """Seconds are the same in every timezone, which is why the sync was the one job
        that stayed right through all of it."""
        agents = self._agents(
            tmp_path / "LaunchAgents", {"com.backglass.sync": (None, (3, 0))}
        )
        monkeypatch.setattr(state_mod, "LAUNCH_AGENTS_DIR", agents)
        schedule = state_mod.collect(conn, settings).sections["schedule"]
        assert schedule["drifting"].value == []
        assert "every 1800s" in schedule["jobs"].value["com.backglass.sync"]

    def test_no_jobs_installed_says_so_rather_than_reporting_health(
        self, conn: sqlite3.Connection, settings: Settings, tmp_path, monkeypatch
    ) -> None:
        """Rule 2 of this module: a probe with nothing to look at reports unknown. An
        empty `drifting` list on a machine with no schedule at all is the confident
        answer assembled from a missing input that this file exists to prevent."""
        empty = tmp_path / "LaunchAgents"
        empty.mkdir()
        monkeypatch.setattr(state_mod, "LAUNCH_AGENTS_DIR", empty)
        schedule = state_mod.collect(conn, settings).sections["schedule"]
        assert schedule["jobs"].unknown is not None
        assert "drifting" not in schedule
