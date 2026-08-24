"""The command palette: the operational half of the CLI, without a terminal.

The owner's directive (2026-08-24). What is pinned here is mostly not "the button works"
— it is the set of things that would make the palette a liability rather than a
convenience: a destructive verb appearing in it, a browser reaching something other than
a registry key, a command's bad news rendering as success, or a finished job polling the
server until the laptop dies.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from backglass.config import Settings
from backglass.web import commands as commands_mod
from backglass.web.jobs import DONE, FAILED, Runner


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from backglass.web.app import create_app

    del conn
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


def _finish(runner: Runner, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = runner.current
        if job is not None and not job.running:
            return
        time.sleep(0.01)
    raise AssertionError("the job never finished")


class TestWhatThePaletteOffers:
    #: The verbs the owner ruled out. Data loss or legal weight (docs/08), and a fuzzy
    #: search that puts them one keystroke from a mis-click buys nothing the terminal
    #: does not already give.
    FORBIDDEN = ("purge-boundary", "prune", "restore", "scrub", "purge")

    def test_no_destructive_command_is_reachable(self) -> None:
        keys = set(commands_mod.BY_KEY)
        assert not keys & set(self.FORBIDDEN), sorted(keys & set(self.FORBIDDEN))

    def test_every_command_says_what_it_costs(self) -> None:
        # "sync" and "backup" look equally cheap in a list and are not. A blurb that does
        # not exist is a command the owner runs without knowing what it will do.
        for c in commands_mod.COMMANDS:
            assert c.blurb.strip(), c.key
            assert c.blurb.strip().endswith("."), f"{c.key}: blurbs are sentences"

    def test_the_registry_is_the_only_lookup(self) -> None:
        """The whole security story. `get` maps a key to a callable and can express
        nothing else — no shell, no argument list, no import name."""
        assert commands_mod.get("loop") is not None
        for hostile in ("../../etc/passwd", "loop; rm -rf /", "backglass.sync", ""):
            assert commands_mod.get(hostile) is None


class TestTheRunner:
    def test_it_runs_the_command_and_keeps_its_words(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        runner = Runner()
        cmd = commands_mod.Command("t", "T", "does a thing.",
                                   lambda c, s: commands_mod.Result(("did a thing",)))
        job, refusal = runner.start(cmd, settings)
        assert refusal is None and job is not None
        _finish(runner)
        assert runner.current.status == DONE          # type: ignore[union-attr]
        assert runner.current.lines == ["did a thing"]  # type: ignore[union-attr]

    def test_a_second_start_is_refused_by_name_rather_than_queued(
        self, settings: Settings
    ) -> None:
        """A queue would let a distracted owner stack six syncs behind one, each of which
        skips on `run_lock` anyway. The honest answer names what is running."""
        gate = __import__("threading").Event()
        runner = Runner()
        slow = commands_mod.Command("slow", "Slow one", "waits.",
                                    lambda c, s: (gate.wait(2), commands_mod.Result(()))[1])
        runner.start(slow, settings)
        job, refusal = runner.start(commands_mod.BY_KEY["logic"], settings)
        assert job is None
        assert refusal == "Slow one is still running"
        gate.set()
        _finish(runner)

    def test_a_command_that_raises_is_recorded_not_swallowed(
        self, settings: Settings
    ) -> None:
        def boom(c: object, s: object) -> commands_mod.Result:
            raise RuntimeError("the endpoint is down")

        runner = Runner()
        runner.start(commands_mod.Command("b", "B", "breaks.", boom), settings)
        _finish(runner)
        job = runner.current
        assert job is not None
        assert job.status == FAILED and job.error == "RuntimeError: the endpoint is down"
        assert job.ok is False

    def test_bad_news_is_not_a_crash(self, settings: Settings) -> None:
        """`ok` and `status` answer different questions and merging them loses one.

        A `sync` that finished degraded, or a `state` with a failing verdict, is a
        command that RAN and is reporting something true. Rendering that as a crash
        teaches the owner to ignore crashes; rendering a crash as bad news hides it.
        """
        runner = Runner()
        runner.start(
            commands_mod.Command("n", "N", "reports.",
                                 lambda c, s: commands_mod.Result(("2 checks fail",), ok=False)),
            settings,
        )
        _finish(runner)
        job = runner.current
        assert job is not None
        assert job.status == DONE and job.ok is False and job.error is None

    def test_it_runs_on_its_own_connection(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The request's connection belongs to a request that has already returned, and
        SQLite objects are not shareable across threads by default."""
        seen: list[int] = []

        def note(c: sqlite3.Connection, s: Settings) -> commands_mod.Result:
            seen.append(id(c))
            return commands_mod.Result(())

        runner = Runner()
        runner.start(commands_mod.Command("c", "C", "notes.", note), settings)
        _finish(runner)
        assert seen and seen[0] != id(conn)


class TestTheRoutes:
    def test_the_palette_is_on_every_page(self, client) -> None:  # type: ignore[no-untyped-def]
        # It is an overlay rather than a page because a command is something the owner
        # reaches for mid-task; navigating away from what prompted it is the wrong shape.
        assert 'id="cmdk"' in client.get("/").text

    def test_the_listing_names_every_command(self, client) -> None:  # type: ignore[no-untyped-def]
        body = client.get("/commands").text
        for c in commands_mod.COMMANDS:
            assert f'data-cmd="{c.key}"' in body

    def test_an_unknown_key_is_a_404_and_starts_nothing(self, client) -> None:  # type: ignore[no-untyped-def]
        assert client.post("/commands/rm-rf/run").status_code == 404
        assert "running" not in client.get("/commands/running").text

    def test_a_finished_job_stops_polling(
        self, client, settings: Settings  # type: ignore[no-untyped-def]
    ) -> None:
        """The trigger is emitted only while the job runs. Without that, a dashboard left
        open overnight is a request every 700ms until morning for a job that ended at
        nine."""
        client.post("/commands/logic/run")
        for _ in range(500):
            body = client.get("/commands/running").text
            if "running…" not in body:
                assert "hx-trigger" not in body, body
                return
            time.sleep(0.01)
        raise AssertionError("logic never finished")

    def test_the_running_panel_shows_the_commands_own_words(
        self, client  # type: ignore[no-untyped-def]
    ) -> None:
        client.post("/commands/logic/run")
        for _ in range(500):
            body = client.get("/commands/running").text
            if "running…" not in body:
                assert "nothing the record contradicts" in body or "disposed of" in body
                return
            time.sleep(0.01)
        raise AssertionError("logic never finished")
