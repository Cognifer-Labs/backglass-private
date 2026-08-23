"""The loop: one declared list, one lock, and a failure that does not take the rest with it.

Before `backglass/loop.py` these five passes were a hundred lines of `try/except: pass`
inside `sync_command`. Everything pinned here is a property that shape could not have:
that there *is* a list, that both callers can run the same one, that the lock covers all
of it rather than the first entry, and that a pass which raises is a recorded failure
rather than a silence indistinguishable from having nothing to do.

The order test is not a tidy-up guard. `logic` before `questions` is a decision — a
question the checker mooted is one the owner never reads — and a diff that swaps them
would otherwise pass every other test in the suite.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from backglass import loop
from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID
from backglass.sync import SyncLocked

PHOENIX = "America/Phoenix"
TODAY = date(2026, 8, 18)
#: Before `plan_at` and before the notify window opens, so the clock-driven passes have
#: nothing owed and the test is about the loop rather than about the morning.
BEFORE_DAWN = datetime(2026, 8, 18, 3, 12, tzinfo=ZoneInfo(PHOENIX))


@pytest.fixture
def sett(settings: Settings) -> Settings:
    return settings.model_copy(update={"default_tz": PHOENIX, "confidence_threshold": 0.7})


def a_pass(name: str, ran: list[str], *, lines: list[str] | None = None) -> loop.Pass:
    def fn(_conn: sqlite3.Connection, _settings: Settings, _now: datetime) -> list[str]:
        ran.append(name)
        return list(lines or [])

    return loop.Pass(name, loop.ALWAYS, spends=False, fn=fn)


def a_boom(name: str, ran: list[str]) -> loop.Pass:
    def fn(_conn: sqlite3.Connection, _settings: Settings, _now: datetime) -> list[str]:
        ran.append(name)
        raise RuntimeError("detector is down")

    return loop.Pass(name, loop.ALWAYS, spends=False, fn=fn)


class TestRegistry:
    def test_the_five_passes_are_declared_in_the_order_they_have_always_run(self) -> None:
        assert loop.NAMES == ("catchup", "replan", "logic", "questions", "notify")

    def test_logic_runs_before_questions_so_a_mooted_question_is_never_asked(self) -> None:
        # Disposal ahead of detection is the decision increment 8 landed. A swap here
        # costs the owner a question the checker would have thrown out.
        assert loop.NAMES.index("logic") < loop.NAMES.index("questions")

    def test_every_pass_declares_what_drives_it(self) -> None:
        assert {p.trigger for p in loop.PASSES} <= {loop.CLOCK, loop.DATA, loop.ALWAYS}

    def test_the_deterministic_passes_say_they_cannot_spend(self) -> None:
        # Verified by reading them: neither logic.py nor questions.py holds a
        # ModelClient reference. The flag is what increment 5's gate and any future cap
        # audit read instead of re-deriving it.
        spending = {p.name for p in loop.PASSES if p.spends}
        assert spending == {"catchup", "replan"}


class TestRunner:
    def test_runs_every_pass_in_registry_order(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        ran: list[str] = []
        passes = [a_pass("one", ran), a_pass("two", ran), a_pass("three", ran)]

        outcomes = loop.run(conn, sett, now=BEFORE_DAWN, passes=passes)

        assert ran == ["one", "two", "three"]
        assert [o.name for o in outcomes] == ["one", "two", "three"]
        assert {o.status for o in outcomes} == {loop.OK}

    def test_a_pass_that_raises_is_recorded_and_the_rest_still_run(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        # Rule 5's whole point: one dead detector must not cost the notification the day
        # needed. The old shape continued too — but silently, which is the other half.
        ran: list[str] = []
        passes = [a_pass("first", ran), a_boom("broken", ran), a_pass("last", ran)]

        outcomes = loop.run(conn, sett, now=BEFORE_DAWN, passes=passes)

        assert ran == ["first", "broken", "last"]
        broken = next(o for o in outcomes if o.name == "broken")
        assert broken.status == loop.FAILED
        assert broken.error == "RuntimeError: detector is down"
        assert [o.status for o in outcomes if o.name != "broken"] == [loop.OK, loop.OK]

    def test_a_failure_is_not_quiet_even_though_it_prints_no_lines(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        ran: list[str] = []
        [outcome] = loop.run(conn, sett, now=BEFORE_DAWN, passes=[a_boom("x", ran)])
        assert outcome.lines == ()
        assert not outcome.quiet

    def test_a_pass_with_nothing_to_say_says_nothing(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        # Five "nothing to do" lines every half hour is how a log stops being read.
        ran: list[str] = []
        [outcome] = loop.run(conn, sett, now=BEFORE_DAWN, passes=[a_pass("x", ran)])
        assert outcome.quiet and outcome.status == loop.OK

    def test_now_is_resolved_once_and_handed_to_every_pass(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        # Five passes deriving their own `now` can disagree about the day across a
        # midnight boundary, which is the shape of the 2026-08-17 timezone lessons.
        seen: list[datetime] = []

        def record(
            _c: sqlite3.Connection, _s: Settings, now: datetime
        ) -> list[str]:
            seen.append(now)
            return []

        passes = [loop.Pass(f"p{i}", loop.ALWAYS, spends=False, fn=record) for i in range(3)]
        loop.run(conn, sett, now=BEFORE_DAWN, passes=passes)

        assert seen == [BEFORE_DAWN, BEFORE_DAWN, BEFORE_DAWN]

    def test_resolves_now_from_the_owners_zone_when_the_caller_gives_none(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        seen: list[datetime] = []

        def record(
            _c: sqlite3.Connection, _s: Settings, now: datetime
        ) -> list[str]:
            seen.append(now)
            return []

        loop.run(
            conn,
            sett,
            passes=[loop.Pass("p", loop.ALWAYS, spends=False, fn=record)],
        )
        assert str(seen[0].tzinfo) == PHOENIX


def loop_run_lock_target():  # type: ignore[no-untyped-def]
    """`loop.run` imports `run_lock` from `backglass.sync` at call time, so the patch
    target is that module rather than a name bound into `loop`."""
    from backglass import sync as sync_mod

    return sync_mod


class TestTheLock:
    def test_the_whole_loop_is_held_under_one_lock(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Not just the first pass, which is what `sync_command` did.

        `replan` supersedes day plans and ran outside the lock; an app-open catchup
        proposing a plan could interleave with it. Asserted by checking the lock is held
        from inside every pass rather than by asserting a call count.
        """
        from backglass.sync import run_lock

        held: list[bool] = []

        def check(_c: sqlite3.Connection, _s: Settings, _n: datetime) -> list[str]:
            try:
                # Re-entrant for this process, so a nested acquire proving nothing is not
                # the risk. The question is whether a *second process* would be excluded,
                # and `_HELD` carrying this database is exactly that fact.
                from pathlib import Path

                from backglass import sync as sync_mod

                key = str(Path(sett.db_path).expanduser().resolve())
                held.append(key in sync_mod._HELD)
            finally:
                pass
            return []

        passes = [loop.Pass(f"p{i}", loop.ALWAYS, spends=False, fn=check) for i in range(3)]
        loop.run(conn, sett, now=BEFORE_DAWN, passes=passes)

        assert held == [True, True, True]
        # And released after: a loop that leaks the lock starves the next sync.
        with run_lock(sett):
            pass

    def test_a_contended_lock_skips_the_loop_rather_than_blocking(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ran: list[str] = []

        def locked(_settings: Settings):  # type: ignore[no-untyped-def]
            raise SyncLocked("another sync is already running (pid 42)")

        monkeypatch.setattr(loop_run_lock_target(), "run_lock", locked)

        outcomes = loop.run(
            conn, sett, now=BEFORE_DAWN, passes=[a_pass("one", ran), a_pass("two", ran)]
        )

        assert ran == []
        assert [o.status for o in outcomes] == [loop.SKIPPED, loop.SKIPPED]

    def test_a_skipped_loop_is_recorded_rather_than_silent(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "Skipped because another run holds the lock" and "ran and found nothing" are
        # different facts, and increment 2 writes both. They must not arrive identical.
        def locked(_settings: Settings):  # type: ignore[no-untyped-def]
            raise SyncLocked("another sync is already running (pid 42)")

        monkeypatch.setattr(loop_run_lock_target(), "run_lock", locked)
        outcomes = loop.run(conn, sett, now=BEFORE_DAWN, passes=[a_pass("one", [])])

        assert outcomes[0].error and "pid 42" in outcomes[0].error
        assert not outcomes[0].quiet


class TestIdempotency:
    """Rule 3 on the loop as a whole: a second run with no upstream change writes nothing.

    Seeded with a commitment whose own text reports it done, so the first run genuinely
    writes — a vacuous pass over an empty ledger would assert nothing.
    """

    @pytest.fixture(autouse=True)
    def _no_embedding_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # catchup indexes the retrieval backlog on every run. It is additive by
        # CLAUDE.md's ruling and swallows its own failures, but a test that waits on a
        # connection refused is a slow test for no reading.
        from backglass import search

        monkeypatch.setattr(search, "index", lambda *_a, **_k: 0)

    def test_the_second_run_writes_nothing(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        _reported_done(conn, "Send the updated resume — sent updated resume yesterday")

        loop.run(conn, sett, now=BEFORE_DAWN)
        after_first = _snapshot(conn)

        outcomes = loop.run(conn, sett, now=BEFORE_DAWN)

        assert _snapshot(conn) == after_first
        assert all(o.status != loop.FAILED for o in outcomes), [
            (o.name, o.error) for o in outcomes if o.error
        ]

    def test_the_first_run_actually_did_something(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        # The guard on the test above: if the seed stopped triggering a disposal, the
        # idempotency assertion would pass by writing nothing twice.
        before = _snapshot(conn)
        _reported_done(conn, "Send the updated resume — sent updated resume yesterday")
        loop.run(conn, sett, now=BEFORE_DAWN)
        assert _snapshot(conn) != before


#: The tables the loop can write. Compared whole rather than by count: a supersede that
#: rewrites a row without adding one is exactly the rule-3 violation a count would miss.
WRITTEN = (
    "commitment",
    "decision",
    "open_question",
    "notification",
    "day_plan",
    "plan_block",
    "brief",
)


def _snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = {}
    for table in WRITTEN:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        out[table] = [tuple(r) for r in rows]
    return out


def _reported_done(conn: sqlite3.Connection, what: str) -> int:
    """An obligation whose own text says it happened — the logic checker's first rule."""
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, content_hash, triage_verdict)"
        " VALUES (?, 'apple-mail', 'm-done', ?, '2026-08-01T09:00:00-07:00', 'a@b.com',"
        " 'Subj', 'body', 'h-done', 'keep')",
        (USER_ID, now_iso()),
    )
    source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, estimated_minutes,"
        " estimate_source, confidence, status, source_item_id, created_at)"
        " VALUES (?, 'i_owe', ?, '2026-08-10', 30, 'manual', 0.9, 'open', ?, ?)",
        (USER_ID, what, source_id, now_iso()),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


class TestTheRecord:
    """Rule 5's missing half: a pass that failed leaves a row, and so does a quiet one.

    The quiet rows are not noise. A pass that *stopped being called* leaves no failure
    to find — that is exactly how the 05:45 job went unnoticed for weeks — so the record
    has to answer "when did this last run at all", not only "what went wrong".
    """

    def test_every_pass_leaves_a_row_including_the_quiet_ones(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        ran: list[str] = []
        loop.run(
            conn,
            sett,
            now=BEFORE_DAWN,
            passes=[a_pass("quiet", ran), a_pass("loud", ran, lines=["did a thing"])],
        )
        rows = {r["name"]: r for r in loop.recent(conn)}
        assert set(rows) == {"quiet", "loud"}
        assert rows["quiet"]["status"] == loop.OK and rows["quiet"]["detail"] is None
        assert rows["loud"]["detail"] == "did a thing"

    def test_a_failure_writes_its_exception_into_the_row(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        loop.run(conn, sett, now=BEFORE_DAWN, passes=[a_boom("broken", [])])
        row = loop.recent(conn)[0]
        assert row["status"] == loop.FAILED
        assert row["detail"] == "RuntimeError: detector is down"

    def test_the_row_carries_the_trigger_the_pass_declared(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        # So a later reader can ask "which clock-driven pass is dead" without importing
        # the registry and re-deriving it.
        clock = loop.Pass("c", loop.CLOCK, spends=False, fn=lambda *_a: [])
        loop.run(conn, sett, now=BEFORE_DAWN, passes=[clock])
        assert loop.recent(conn)[0]["trigger"] == loop.CLOCK

    def test_a_lock_skip_is_written_down_rather_than_vanishing(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def locked(_settings: Settings):  # type: ignore[no-untyped-def]
            raise SyncLocked("another sync is already running (pid 42)")

        monkeypatch.setattr(loop_run_lock_target(), "run_lock", locked)
        loop.run(conn, sett, now=BEFORE_DAWN, passes=[a_pass("one", [])])

        row = loop.recent(conn)[0]
        assert row["status"] == loop.SKIPPED and "pid 42" in str(row["detail"])

    def test_a_dry_run_reports_without_leaving_a_trace(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        outcomes = loop.run(
            conn, sett, now=BEFORE_DAWN, passes=[a_pass("x", [])], record=False
        )
        assert outcomes[0].status == loop.OK
        assert loop.recent(conn) == []

    def test_the_row_carries_the_sync_run_it_rode_with(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        loop.run(conn, sett, now=BEFORE_DAWN, passes=[a_pass("x", [])], run_id=77)
        assert loop.recent(conn)[0]["run_id"] == 77

    def test_a_loop_outside_a_sync_records_no_run(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        # The app-open trigger belongs to no sync run, which is why the column is
        # nullable rather than a foreign key that would refuse the row.
        loop.run(conn, sett, now=BEFORE_DAWN, passes=[a_pass("x", [])])
        assert loop.recent(conn)[0]["run_id"] is None

    def test_recording_failure_does_not_break_a_healthy_loop(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The row is the observation, not the work. A ledger that cannot take the
        # observation must not turn a working pass into a reported failure.
        conn.execute("DROP TABLE loop_pass")
        outcomes = loop.run(conn, sett, now=BEFORE_DAWN, passes=[a_pass("x", [])])
        assert outcomes[0].status == loop.OK


class TestHealth:
    def test_a_pass_that_has_never_run_is_named_rather_than_absent(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The whole point. A reader that enumerates the rows cannot see the pass nobody
        called, and that is the failure this increment exists for."""
        assert [p.name for p in loop.health(conn)] == list(loop.NAMES)
        assert all(p.last_ok is None and p.last_status is None for p in loop.health(conn))

    def test_last_ok_is_the_last_success_not_the_last_attempt(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        real = loop.PASSES[2]  # logic — a declared name, so health() looks for it
        good = loop.Pass(real.name, real.trigger, spends=False, fn=lambda *_a: [])
        loop.run(conn, sett, now=BEFORE_DAWN, passes=[good])
        loop.run(conn, sett, now=BEFORE_DAWN, passes=[a_boom(real.name, [])])

        entry = next(p for p in loop.health(conn) if p.name == real.name)
        assert entry.last_ok is not None       # the success is still findable
        assert entry.last_status == loop.FAILED  # and the current state is honest
        assert entry.consecutive_failures == 1

    def test_a_success_clears_the_failing_streak(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        real = loop.PASSES[2]
        loop.run(conn, sett, now=BEFORE_DAWN, passes=[a_boom(real.name, [])])
        loop.run(conn, sett, now=BEFORE_DAWN, passes=[a_boom(real.name, [])])
        assert loop.failing(conn)[0].consecutive_failures == 2

        good = loop.Pass(real.name, real.trigger, spends=False, fn=lambda *_a: [])
        loop.run(conn, sett, now=BEFORE_DAWN, passes=[good])
        assert loop.failing(conn) == []

    def test_a_contended_lock_is_not_counted_as_a_failure(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A skip means the pass was not attempted, so it neither proves health nor
        breaks a streak. Counting it as a failure would alarm on the normal case: two
        launchd firings overlapping a long backfill."""
        real = loop.PASSES[2]
        loop.run(conn, sett, now=BEFORE_DAWN, passes=[a_boom(real.name, [])])

        def locked(_settings: Settings):  # type: ignore[no-untyped-def]
            raise SyncLocked("another sync is already running (pid 42)")

        monkeypatch.setattr(loop_run_lock_target(), "run_lock", locked)
        loop.run(conn, sett, now=BEFORE_DAWN, passes=[a_pass(real.name, [])])

        entry = next(p for p in loop.health(conn) if p.name == real.name)
        assert entry.consecutive_failures == 1  # the skip neither added nor cleared


class TestTheStateVerdict:
    """`backglass state` is where the owner is told to look first, so it is where a dead
    pass has to appear. Two failure shapes, and the second is the one that hides."""

    def _verdict(self, conn: sqlite3.Connection, sett: Settings):  # type: ignore[no-untyped-def]
        from backglass import state as state_mod

        checks = state_mod.verdicts(state_mod.collect(conn, sett), conn, sett)
        return next((v for v in checks if v.name == "every loop pass is succeeding"), None)

    def test_silent_until_the_loop_has_run_once(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        # A ledger where the loop has not run since the migration is not a broken loop,
        # and a red line for it would be a false alarm on every fresh install.
        assert self._verdict(conn, sett) is None

    def test_a_failing_pass_is_named(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        real = loop.PASSES[2]
        loop.run(conn, sett, now=BEFORE_DAWN, passes=[a_boom(real.name, [])])
        verdict = self._verdict(conn, sett)
        assert verdict is not None and verdict.ok is False
        assert real.name in verdict.detail

    def test_a_pass_that_was_never_called_is_named_too(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The one that hides. A pass nobody calls leaves no failure to find — the 05:45
        job was dead for weeks and every surface read green (2026-08-17 lesson)."""
        real = loop.PASSES[2]
        good = loop.Pass(real.name, real.trigger, spends=False, fn=lambda *_a: [])
        loop.run(conn, sett, now=BEFORE_DAWN, passes=[good])

        verdict = self._verdict(conn, sett)
        assert verdict is not None and verdict.ok is False
        assert "notify has never succeeded" in verdict.detail

    def test_green_once_every_pass_has_succeeded(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        every = [
            loop.Pass(p.name, p.trigger, spends=False, fn=lambda *_a: [])
            for p in loop.PASSES
        ]
        loop.run(conn, sett, now=BEFORE_DAWN, passes=every)
        verdict = self._verdict(conn, sett)
        assert verdict is not None and verdict.ok is True


class TestTheExitCode:
    """"Log, surface, continue, exit non-zero" — the last clause was never implemented.

    launchd is the only thing watching this command, and a zero exit is how it decides
    the run was fine. A loop pass throwing for a week exited 0 every time.
    """

    def _sync(  # type: ignore[no-untyped-def]
        self,
        conn: sqlite3.Connection,
        sett: Settings,
        monkeypatch: pytest.MonkeyPatch,
        outcomes: list[loop.Outcome],
    ):
        from typer.testing import CliRunner

        import backglass.__main__ as cli
        from backglass.sync import SyncReport

        monkeypatch.setattr(cli, "get_settings", lambda: sett)
        monkeypatch.setattr(cli, "_open", lambda _s: conn)
        monkeypatch.setattr(cli, "migrate", lambda _c: [])
        monkeypatch.setattr(cli, "_all_connectors", lambda _c, _s: ["a source"])
        monkeypatch.setattr(cli, "_contacts_source", lambda _c, _s: None)
        monkeypatch.setattr(cli, "_build_model_client", lambda _s: None)
        monkeypatch.setattr(cli, "sync", lambda *_a, **_k: SyncReport())
        monkeypatch.setattr(cli.loop, "run", lambda *_a, **_k: outcomes)
        return CliRunner().invoke(cli.app, ["sync"])

    def test_a_failed_pass_makes_the_run_fail(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._sync(
            conn, sett, monkeypatch,
            [loop.Outcome("logic", loop.FAILED, error="OSError: disk")],
        )
        assert result.exit_code == 1, result.output
        assert "loop pass 'logic' failed: OSError: disk" in result.output

    def test_a_quiet_loop_still_exits_clean(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._sync(conn, sett, monkeypatch, [loop.Outcome("logic", loop.OK)])
        assert result.exit_code == 0, result.output

    def test_a_contended_lock_is_not_a_failure(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two launchd firings overlapping a long backfill is the normal case, not a
        # broken machine. Exiting non-zero on it would train the owner to ignore the one
        # signal this increment exists to give them.
        result = self._sync(
            conn, sett, monkeypatch,
            [loop.Outcome("logic", loop.SKIPPED, error="another sync is already running")],
        )
        assert result.exit_code == 0, result.output
