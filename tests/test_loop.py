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


def a_declared(name: str) -> loop.Pass:
    """The registry entry for `name`. Named rather than indexed: `health()` and the gate
    both look a pass up by name, and an index silently means a different pass the next
    time the order changes — which it did on 2026-08-23."""
    return next(p for p in loop.PASSES if p.name == name)


class TestRegistry:
    def test_the_passes_are_declared_in_the_order_they_run(self) -> None:
        assert loop.NAMES == (
            "logic", "questions", "catchup", "replan", "duplicates", "noise", "notify",
        )

    def test_logic_runs_before_questions_so_a_mooted_question_is_never_asked(self) -> None:
        # Disposal ahead of detection is the decision increment 8 landed. A swap here
        # costs the owner a question the checker would have thrown out.
        assert loop.NAMES.index("logic") < loop.NAMES.index("questions")

    def test_the_planning_passes_run_after_the_disposal(self) -> None:
        """The reorder of 2026-08-23, and the invariant the old order violated.

        A logic disposal changes the open set, which changes the planner pool, which
        changes `inputs_fingerprint`. With replan ahead of logic — which is how the chain
        ran for as long as it existed — replan compared today's plan against a world
        logic was about to edit, so the drift it should have caught arrived on the next
        sync thirty minutes later, or at 05:45. Nothing was ever wrong in the ledger; the
        board was simply half an hour stale every time the checker did anything.

        Argument from `tasks/pipeline-audit-2026-08-21.md` §1c, which this loop was built
        without knowing existed.
        """
        for planner_pass in ("catchup", "replan"):
            assert loop.NAMES.index("logic") < loop.NAMES.index(planner_pass)
            assert loop.NAMES.index("questions") < loop.NAMES.index(planner_pass)

    def test_the_card_passes_run_before_notify_and_after_the_planning(self) -> None:
        # Before notify because its questions-waiting decider counts what they raise;
        # after the planning passes because a card changes nothing the planner reads —
        # the merge happens on the owner's answer, not when the card goes up.
        for cards in ("duplicates", "noise"):
            assert loop.NAMES.index("replan") < loop.NAMES.index(cards)
            assert loop.NAMES.index(cards) < loop.NAMES.index("notify")

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

    def test_it_reaches_a_fixed_point_and_stays_there(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Convergence, which is the property rule 3 is really asking about here.

        The fixture case above settles on the second run. On a copy of the live ledger
        (2026-08-23, 10,563 items) it settles on the **third**: run 2 wrote one more
        `decision` row — a machine disposal of a question run 1's own passes had left
        behind — and runs 3 through 6 wrote nothing at all. That is a settling cost, not
        an oscillation, and the difference matters: an oscillation would write a decision
        row every thirty minutes forever, which is the shape of the 13-briefs-a-day defect
        in tasks/lessons.md.

        Pinned as its own property because the two-run assertion cannot see it. A loop
        that converged on run 50 would pass a test that only compares 1 and 2 on a fixture
        small enough to settle immediately.
        """
        _reported_done(conn, "Send the updated resume — sent updated resume yesterday")
        seen = []
        for _ in range(4):
            loop.run(conn, sett, now=BEFORE_DAWN)
            seen.append(_snapshot(conn))
        assert seen[-1] == seen[-2] == seen[-3], "the loop has not stopped writing"

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

    def test_the_row_carries_both_clocks_and_they_are_not_the_same_one(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """`local_date` is the owner's day; the timestamps are wall-clock UTC.

        Storing one and deriving the other was the first version and it broke the
        once-a-day gate in both directions: `finished_at` is stamped from the wall clock
        whatever day is being processed, and even frozen, 20:00 Phoenix is already
        tomorrow in UTC. They answer different questions, so the row carries both.
        """
        evening = BEFORE_DAWN.replace(hour=20)  # 2026-08-19T03:00Z — a different UTC date
        loop.run(conn, sett, now=evening, passes=[a_pass("x", [])])

        row = loop.recent(conn)[0]
        assert row["local_date"] == "2026-08-18"
        assert str(row["finished_at"])[:10] != "2026-08-18"  # the wall clock, not the day

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
        real = a_declared("logic")
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
        real = a_declared("logic")
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
        real = a_declared("logic")
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
        real = a_declared("logic")
        loop.run(conn, sett, now=BEFORE_DAWN, passes=[a_boom(real.name, [])])
        verdict = self._verdict(conn, sett)
        assert verdict is not None and verdict.ok is False
        assert real.name in verdict.detail

    def test_a_pass_that_was_never_called_is_named_too(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The one that hides. A pass nobody calls leaves no failure to find — the 05:45
        job was dead for weeks and every surface read green (2026-08-17 lesson)."""
        real = a_declared("logic")
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
        # `_build_model_client` gained an optional conn on fix/schedule-canvas-overflow
        # (model health reads `model_call`); the stub takes whatever it is handed.
        monkeypatch.setattr(cli, "_build_model_client", lambda *_a, **_k: None)
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


class TestTheCommand:
    """`backglass loop` — one-shot, never a scheduler.

    It exists for the two cases the automatic triggers do not cover: reading where the
    loop stands, and nudging it after changing something the passes read. Everything
    else in goal 3 is about the owner not needing it.
    """

    def _run(  # type: ignore[no-untyped-def]
        self,
        conn: sqlite3.Connection,
        sett: Settings,
        monkeypatch: pytest.MonkeyPatch,
        *args: str,
    ):
        from typer.testing import CliRunner

        import backglass.__main__ as cli

        monkeypatch.setattr(cli, "get_settings", lambda: sett)
        monkeypatch.setattr(cli, "_open", lambda _s: conn)
        monkeypatch.setattr(cli, "migrate", lambda _c: [])
        return CliRunner().invoke(cli.app, ["loop", *args])

    def test_it_runs_the_loop_and_reports(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backglass import search

        monkeypatch.setattr(search, "index", lambda *_a, **_k: 0)
        result = self._run(conn, sett, monkeypatch)
        assert result.exit_code == 0, result.output
        # Every pass left a row, which is what makes the loop readable afterwards.
        assert {r["name"] for r in loop.recent(conn)} == set(loop.NAMES)

    def test_only_runs_the_named_passes(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._run(conn, sett, monkeypatch, "--only", "logic", "--only", "questions")
        assert result.exit_code == 0, result.output
        assert {r["name"] for r in loop.recent(conn)} == {"logic", "questions"}

    def test_a_misspelled_pass_is_an_error_not_a_silent_no_op(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Silence after `--only notifiy` reads as "nothing to do" and the owner walks
        # away believing the pass ran.
        result = self._run(conn, sett, monkeypatch, "--only", "notifiy")
        assert result.exit_code == 2
        assert "unknown pass" in result.output and "notify" in result.output
        assert loop.recent(conn) == []

    def test_a_failing_pass_makes_the_command_fail(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import backglass.__main__ as cli

        monkeypatch.setattr(
            cli.loop, "run",
            lambda *_a, **_k: [loop.Outcome("logic", loop.FAILED, error="OSError: disk")],
        )
        result = self._run(conn, sett, monkeypatch)
        assert result.exit_code == 1
        assert "loop pass 'logic' failed" in result.output

    def test_a_quiet_loop_says_so_rather_than_printing_nothing(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Someone typed this and is waiting on it. A blank terminal is not an answer.
        import backglass.__main__ as cli

        monkeypatch.setattr(cli.loop, "run", lambda *_a, **_k: [loop.Outcome("logic", loop.OK)])
        result = self._run(conn, sett, monkeypatch)
        assert "nothing owed" in result.output

    def test_a_contended_lock_is_said_out_loud(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import backglass.__main__ as cli

        monkeypatch.setattr(
            cli.loop, "run",
            lambda *_a, **_k: [
                loop.Outcome("logic", loop.SKIPPED, error="another sync is already running")
            ],
        )
        result = self._run(conn, sett, monkeypatch)
        assert result.exit_code == 0
        assert "skipped: another sync is already running" in result.output


class TestTheDryRun:
    def test_it_reads_where_each_pass_stands_and_writes_nothing(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not "run it and roll back". These passes deliver notifications and supersede
        plans; a rehearsal that called them would do both, which is not what dry run
        means to anyone reading the word."""
        from typer.testing import CliRunner

        import backglass.__main__ as cli

        real = a_declared("logic")
        loop.run(conn, sett, now=BEFORE_DAWN, passes=[a_boom(real.name, [])])
        before = loop.recent(conn)

        monkeypatch.setattr(cli, "get_settings", lambda: sett)
        monkeypatch.setattr(cli, "_open", lambda _s: conn)
        monkeypatch.setattr(cli, "migrate", lambda _c: [])
        result = CliRunner().invoke(cli.app, ["loop", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert [tuple(r) for r in loop.recent(conn)] == [tuple(r) for r in before]
        assert "never run" in result.output          # the four that have not
        assert "failing ×1" in result.output         # and the one that is
        assert "RuntimeError: detector is down" in result.output

    def test_it_names_every_declared_pass_including_the_ones_never_called(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        import backglass.__main__ as cli

        monkeypatch.setattr(cli, "get_settings", lambda: sett)
        monkeypatch.setattr(cli, "_open", lambda _s: conn)
        monkeypatch.setattr(cli, "migrate", lambda _c: [])
        result = CliRunner().invoke(cli.app, ["loop", "--dry-run"])
        for name in loop.NAMES:
            assert name in result.output


#: 08:00 Phoenix on the same Tuesday: past `plan_at` and `brief_at`, so the morning
#: surfaces are genuinely owed and the app-open trigger has real work to do.
MORNING = datetime(2026, 8, 18, 8, 0, tzinfo=ZoneInfo(PHOENIX))


def _plannable(conn: sqlite3.Connection, what: str, *, minutes: int = 45) -> int:
    """An open obligation with an estimate, so the planner has something to place."""
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


class TestTheAppOpenTrigger:
    """Opening the app runs the whole loop, not a third of it.

    The trigger used to live in `catchup` because catchup was all it fired, so the owner
    opening the dashboard got the morning surfaces and none of the disposal, detection or
    notification the CLI ran every half hour. Widening it is safe because of what the
    passes already are — idempotent, owed-gated, ask-once — which `TestIdempotency`
    asserts over the whole loop rather than pass by pass.
    """

    @pytest.fixture(autouse=True)
    def _no_embedding_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from backglass import search

        monkeypatch.setattr(search, "index", lambda *_a, **_k: 0)

    def test_it_runs_every_pass_and_commits(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """On its own connection, on a thread, so an uncommitted write would be invisible
        to every reader including the page that triggered it."""
        _plannable(conn, "email the signed waivers")
        conn.commit()

        outcomes = loop.on_open(sett, now=MORNING)

        assert [o.name for o in outcomes] == list(loop.NAMES)
        assert [o.name for o in outcomes if o.status == loop.FAILED] == []
        fresh = sqlite3.connect(sett.db_path)
        try:
            planned = fresh.execute(
                "SELECT COUNT(*) FROM day_plan WHERE local_date = ?", ("2026-08-18",)
            ).fetchone()[0]
            passes = fresh.execute("SELECT COUNT(*) FROM loop_pass").fetchone()[0]
        finally:
            fresh.close()
        assert planned == 1
        assert passes == len(loop.NAMES)  # the record committed with the work

    def test_it_skips_while_another_process_holds_the_lock(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """An app opened at :18 while the timer's sync is mid-run must not propose the
        same day a second time — one plan superseded a second later, both paid for.

        Taken through a second file descriptor rather than through `run_lock`, because
        that is the case being tested: flock excludes by open file description and
        `run_lock` is deliberately reentrant within a process. The collision that matters
        is between the launchd sync and this dashboard, which are two processes.
        """
        import fcntl
        from pathlib import Path

        from backglass.plan import planner

        _plannable(conn, "email the signed waivers")
        conn.commit()

        db = Path(sett.db_path)
        held = (db.parent / f"{db.name}.sync-lock").open("a+")
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            outcomes = loop.on_open(sett, now=MORNING)
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
            held.close()

        assert {o.status for o in outcomes} == {loop.SKIPPED}
        assert planner.current_plan_id(conn, date(2026, 8, 18)) is None

    def test_a_second_open_does_not_start_a_second_loop(
        self, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two tabs, one loop. The in-process guard is held for the thread's whole life,
        so the second call returns without starting anything. It is not redundant with
        `run_lock`: that one excludes other processes, this one excludes the tab next to
        it before either reaches the ledger."""
        started: list[str] = []

        class FakeThread:
            def __init__(self, *_a: object, **kwargs: object) -> None:
                self.target = kwargs["target"]

            def start(self) -> None:
                started.append("go")

        monkeypatch.setattr(loop.threading, "Thread", FakeThread)
        loop.spawn_on_open(sett)
        loop.spawn_on_open(sett)
        assert started == ["go"]
        loop._running.release()

    def test_a_broken_loop_never_reaches_the_page(
        self, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Rule 5: the dashboard renders, the ledger is correct, and `state` says what is
        # missing. A trigger that raised into the request path would be a 500 on the page
        # the owner opened to find out what today looks like.
        def boom(*_a: object, **_k: object) -> list[loop.Outcome]:
            raise OSError("no database")

        monkeypatch.setattr(loop, "on_open", boom)
        loop.spawn_on_open(sett)  # daemon thread, swallows and logs


class TestDuplicatesLeavesTheTerminal:
    """73 clusters over 386 open commitments that no page has ever shown.

    `backglass duplicates` found them from the first day it existed; nothing else called
    it, so the only way to see a scholarship acceptance written down five ways — and
    costing five slots of a real day — was to type a command nobody types unprompted.
    """

    def _cluster(self, conn: sqlite3.Connection, what: str, n: int = 2) -> list[int]:
        ids = []
        for i in range(n):
            conn.execute(
                "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
                " occurred_at, title, body_text, content_hash, triage_verdict,"
                " extraction_version) VALUES (?, 'apple-mail', ?,"
                " '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z', ?, ?, ?, 'keep',"
                " 'manual')",
                (USER_ID, f"dup-{what}-{i}", what, what, f"h-{what}-{i}"),
            )
            item = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            conn.execute(
                "INSERT INTO commitment (user_id, direction, what, confidence, status,"
                " estimated_minutes, estimate_source, source_item_id, created_at)"
                " VALUES (?, 'i_owe', ?, 0.9, 'open', 30, 'manual', ?, ?)",
                (USER_ID, what, item, now_iso()),
            )
            ids.append(int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]))
        return ids

    def test_the_clusters_arrive_as_cards(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        from backglass import questions as questions_mod

        self._cluster(conn, "Submit the hospice volunteer application")
        loop.run(conn, sett, now=BEFORE_DAWN, passes=loop.by_name(["duplicates"]))

        cards = [q for q in questions_mod.open_questions(conn) if q["kind"] == "duplicate"]
        assert len(cards) == 1
        assert "same promise" in cards[0]["question"]
        assert cards[0]["options"] == [dup_mod().DUP_SAME, dup_mod().DUP_APART]

    def test_it_runs_once_a_day_and_says_why_it_skipped(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The gate is the reason this is a pass rather than another detector: the
        clustering is O(n²) and costs ~3.7 s on the live ledger, which no index touches
        because the pairwise comparison *is* the cost."""
        self._cluster(conn, "Submit the hospice volunteer application")
        only = loop.by_name(["duplicates"])

        first = loop.run(conn, sett, now=BEFORE_DAWN, passes=only)
        assert first[0].status == loop.OK

        later = BEFORE_DAWN.replace(hour=14)
        second = loop.run(conn, sett, now=later, passes=only)
        assert second[0].status == loop.SKIPPED
        assert "already ran today" in str(second[0].error)

    def test_the_next_day_it_runs_again(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        only = loop.by_name(["duplicates"])
        loop.run(conn, sett, now=BEFORE_DAWN, passes=only)
        tomorrow = BEFORE_DAWN.replace(day=BEFORE_DAWN.day + 1)
        assert loop.run(conn, sett, now=tomorrow, passes=only)[0].status == loop.OK

    def test_the_gate_is_the_owners_local_day_not_utc(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The owner moves between UTC-7 and UTC+05:30. A run at 20:00 Phoenix is already
        tomorrow in UTC, so a date-prefix comparison would open the gate at dinner and
        run the 3.7 s pass twice on one of the owner's days."""
        only = loop.by_name(["duplicates"])
        evening = BEFORE_DAWN.replace(hour=20)  # 2026-08-19T03:00Z — a different UTC date
        assert loop.run(conn, sett, now=evening, passes=only)[0].status == loop.OK

        later = BEFORE_DAWN.replace(hour=22)
        assert loop.run(conn, sett, now=later, passes=only)[0].status == loop.SKIPPED

    def test_a_failed_run_does_not_close_the_gate(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The gate asks whether the pass SUCCEEDED today. A pass that threw has not done
        # its work, and locking it out until tomorrow would turn one bad run into a day
        # without the surface.
        def boom(*_a: object, **_k: object) -> list[object]:
            raise RuntimeError("clustering blew up")

        monkeypatch.setattr(dup_mod(), "questions_for", boom)
        only = loop.by_name(["duplicates"])
        assert loop.run(conn, sett, now=BEFORE_DAWN, passes=only)[0].status == loop.FAILED

        monkeypatch.undo()
        later = BEFORE_DAWN.replace(hour=9)
        assert loop.run(conn, sett, now=later, passes=only)[0].status == loop.OK

    def test_nothing_is_collapsed_without_the_owner(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """A row that vanishes with nothing saying why is the failure four lessons cover.
        The pass asks; `--apply` on the command stays a deliberate keystroke."""
        ids = self._cluster(conn, "Submit the hospice volunteer application")
        loop.run(conn, sett, now=BEFORE_DAWN, passes=loop.by_name(["duplicates"]))
        still_open = conn.execute(
            "SELECT COUNT(*) AS n FROM commitment WHERE status = 'open'"
        ).fetchone()["n"]
        assert still_open == len(ids)


class TestAnsweringACluster:
    def _card(self, conn: sqlite3.Connection, sett: Settings) -> tuple[int, list[int]]:
        from backglass import questions as questions_mod

        helper = TestDuplicatesLeavesTheTerminal()
        ids = helper._cluster(conn, "Submit the hospice volunteer application")
        loop.run(conn, sett, now=BEFORE_DAWN, passes=loop.by_name(["duplicates"]))
        card = next(
            q for q in questions_mod.open_questions(conn) if q["kind"] == "duplicate"
        )
        return int(card["id"]), ids

    def test_one_promise_merges_into_the_id_the_card_named(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        from backglass import questions as questions_mod

        card_id, ids = self._card(conn, sett)
        questions_mod.answer(conn, sett, card_id, option=dup_mod().DUP_SAME)

        rows = {
            int(r["id"]): dict(r)
            for r in conn.execute("SELECT id, status, superseded_by FROM commitment")
        }
        keep = min(ids)
        assert rows[keep]["status"] == "open"
        for other in ids:
            if other != keep:
                assert rows[other]["status"] == "superseded"
                assert rows[other]["superseded_by"] == keep

    def test_keeping_them_apart_is_remembered_so_it_is_never_asked_again(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        from backglass import questions as questions_mod

        card_id, ids = self._card(conn, sett)
        questions_mod.answer(conn, sett, card_id, option=dup_mod().DUP_APART)

        pairs = conn.execute(
            "SELECT low_id, high_id FROM commitment_distinct WHERE user_id = ?",
            (USER_ID,),
        ).fetchall()
        assert {(int(r["low_id"]), int(r["high_id"])) for r in pairs} == {
            (min(ids), other) for other in ids if other != min(ids)
        }
        assert all(
            str(r["status"]) == "open"
            for r in conn.execute("SELECT status FROM commitment")
        )

    def test_the_owners_own_words_change_nothing(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        # "Never guess in the meantime": an answer this hook does not recognise is
        # recorded and acts on nothing.
        from backglass import questions as questions_mod

        card_id, ids = self._card(conn, sett)
        questions_mod.answer(conn, sett, card_id, text="two different scholarships")
        assert all(
            str(r["status"]) == "open"
            for r in conn.execute("SELECT status FROM commitment")
        )
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM commitment_distinct"
        ).fetchone()["n"] == 0


def dup_mod():  # type: ignore[no-untyped-def]
    from backglass import duplicates

    return duplicates


class TestTheCardOnThePage:
    """The claim increment 6 makes: this is answerable without a terminal.

    Asserted end to end rather than at the question row, because "it writes an
    open_question" was already true of things the page never showed — the whole defect
    was a surface that existed and was unreachable.
    """

    @pytest.fixture
    def client(self, conn: sqlite3.Connection, sett: Settings):  # type: ignore[no-untyped-def]
        from fastapi.testclient import TestClient

        from backglass.web.app import create_app

        del conn
        return TestClient(create_app(sett), base_url="http://127.0.0.1:8765")

    def test_a_cluster_is_readable_and_answerable_at_ask(
        self, client, conn: sqlite3.Connection, sett: Settings  # type: ignore[no-untyped-def]
    ) -> None:
        helper = TestDuplicatesLeavesTheTerminal()
        ids = helper._cluster(conn, "Submit the hospice volunteer application")
        loop.run(conn, sett, now=BEFORE_DAWN, passes=loop.by_name(["duplicates"]))

        body = client.get("/ask").text
        assert "same promise" in body
        assert f"#{min(ids)}" in body                     # every member is named
        assert dup_mod().DUP_SAME in body                 # and both answers are buttons
        assert dup_mod().DUP_APART in body

        qid = int(
            conn.execute(
                "SELECT id FROM open_question WHERE kind = 'duplicate'"
            ).fetchone()["id"]
        )
        client.post(
            f"/ask/{qid}/answer",
            data={"option": dup_mod().DUP_SAME},
            follow_redirects=False,
        )

        statuses = {
            int(r["id"]): str(r["status"])
            for r in conn.execute("SELECT id, status FROM commitment")
        }
        assert statuses[min(ids)] == "open"
        assert all(statuses[i] == "superseded" for i in ids if i != min(ids))


class TestNoiseLeavesTheTerminal:
    """Twenty-one senders on the live ledger, reachable only by typing a command.

    The alternative already in the codebase is `noise_auto_promote`, which stops reading
    a sender on the machine's own judgement. This increment does not turn that on: it
    makes the click cheap enough that the flag has nothing left to offer. A card leaves a
    decision row naming who decided; the flag makes "why did I stop seeing mail from my
    landlord" unanswerable.
    """

    def _barren_sender(self, conn: sqlite3.Connection, address: str, n: int = 6) -> None:
        for i in range(n):
            conn.execute(
                "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
                " occurred_at, author, title, body_text, content_hash, triage_verdict,"
                " triage_reason) VALUES (?, 'apple-mail', ?, '2026-08-10T00:00:00Z',"
                " ?, ?, ?, 'body', ?, 'drop', 'model: marketing blast')",
                (USER_ID, f"n-{address}-{i}", f"2026-08-{10 + i:02d}T09:00:00-07:00",
                 address, f"Deal {i}", f"hn-{address}-{i}"),
            )

    def test_a_barren_sender_becomes_a_card(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        from backglass import questions as questions_mod
        from backglass.extract import noise as noise_mod

        self._barren_sender(conn, "deals@shop.example")
        loop.run(conn, sett, now=BEFORE_DAWN, passes=loop.by_name(["noise"]))

        cards = [q for q in questions_mod.open_questions(conn) if q["kind"] == "noise"]
        assert len(cards) == 1
        assert "deals@shop.example" in cards[0]["question"]
        assert cards[0]["options"] == [noise_mod.NOISE_STOP, noise_mod.NOISE_KEEP]

    def test_the_card_says_what_its_evidence_is_about(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Found by rendering the card against the owner's real ledger, not by a fixture.

        `deadlines@scholarships.com` came back with "Most recent subject: Dharsan- Your
        Approaching Scholarship Deadlines" and, on the next line, "Why triage dropped it:
        no letters or digits". Both true — the rule reads the *extracted body*, and an
        HTML-only marketing mail extracts to nothing — and together they read as a
        contradiction. On a question whose answer permanently stops mail arriving, a card
        that looks like it is reasoning from something false is worse than no card.
        """
        from backglass.extract import noise as noise_mod

        self._barren_sender(conn, "deals@shop.example")
        [card] = noise_mod.questions_for(conn, sett)

        assert "dropped one of them" in card.detail  # a message, not the sender
        assert "extracted body text, not the subject" in card.detail

    def test_the_card_shows_days_rather_than_instants(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        from backglass.extract import noise as noise_mod

        self._barren_sender(conn, "deals@shop.example")
        [card] = noise_mod.questions_for(conn, sett)

        seen = next(ln for ln in card.detail.splitlines() if ln.startswith("Seen "))
        assert "T" not in seen and "+00:00" not in seen, seen

    def test_stopping_a_sender_promotes_it(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        from backglass import questions as questions_mod
        from backglass.extract import noise as noise_mod

        self._barren_sender(conn, "deals@shop.example")
        loop.run(conn, sett, now=BEFORE_DAWN, passes=loop.by_name(["noise"]))
        qid = int(
            conn.execute(
                "SELECT id FROM open_question WHERE kind = 'noise'"
            ).fetchone()["id"]
        )
        questions_mod.answer(conn, sett, qid, option=noise_mod.NOISE_STOP)

        rows = conn.execute(
            "SELECT value, promoted_by FROM learned_noise WHERE user_id = ?", (USER_ID,)
        ).fetchall()
        assert [(str(r["value"]), str(r["promoted_by"])) for r in rows] == [
            ("deals@shop.example", "owner")
        ]

    def test_keeping_it_writes_no_row_and_is_never_asked_again(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """Ask-once is what makes "keep reading it" durable. A `learned_noise` row saying
        "not noise" would be a second store for the same fact that can disagree with the
        first."""
        from backglass import questions as questions_mod
        from backglass.extract import noise as noise_mod

        self._barren_sender(conn, "deals@shop.example")
        only = loop.by_name(["noise"])
        loop.run(conn, sett, now=BEFORE_DAWN, passes=only)
        qid = int(
            conn.execute(
                "SELECT id FROM open_question WHERE kind = 'noise'"
            ).fetchone()["id"]
        )
        questions_mod.answer(conn, sett, qid, option=noise_mod.NOISE_KEEP)

        assert conn.execute(
            "SELECT COUNT(*) AS n FROM learned_noise"
        ).fetchone()["n"] == 0

        tomorrow = BEFORE_DAWN.replace(day=BEFORE_DAWN.day + 1)
        loop.run(conn, sett, now=tomorrow, passes=only)
        assert [q for q in questions_mod.open_questions(conn) if q["kind"] == "noise"] == []

    def test_a_sender_that_earned_its_place_since_the_card_is_not_promoted(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The counts move between asking and answering. Promoting the `Candidate` the
        card was built from would record evidence that is no longer true and stop reading
        a sender that has since produced something real."""
        from backglass import questions as questions_mod
        from backglass.extract import noise as noise_mod

        self._barren_sender(conn, "deals@shop.example")
        loop.run(conn, sett, now=BEFORE_DAWN, passes=loop.by_name(["noise"]))
        qid = int(
            conn.execute(
                "SELECT id FROM open_question WHERE kind = 'noise'"
            ).fetchone()["id"]
        )

        # It produced a real commitment after the card was raised.
        item = int(
            conn.execute(
                "SELECT id FROM source_item WHERE author = ? LIMIT 1",
                ("deals@shop.example",),
            ).fetchone()["id"]
        )
        conn.execute(
            "INSERT INTO commitment (user_id, direction, what, confidence, status,"
            " estimated_minutes, estimate_source, source_item_id, created_at)"
            " VALUES (?, 'i_owe', 'Return the mattress', 0.9, 'open', 30, 'manual', ?, ?)",
            (USER_ID, item, now_iso()),
        )

        questions_mod.answer(conn, sett, qid, option=noise_mod.NOISE_STOP)
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM learned_noise"
        ).fetchone()["n"] == 0

    def test_it_runs_once_a_day(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        only = loop.by_name(["noise"])
        assert loop.run(conn, sett, now=BEFORE_DAWN, passes=only)[0].status == loop.OK
        later = BEFORE_DAWN.replace(hour=14)
        assert loop.run(conn, sett, now=later, passes=only)[0].status == loop.SKIPPED

    def test_the_auto_promote_flag_is_still_off(self, sett: Settings) -> None:
        # The point of the card is that the flag has nothing left to offer. If a future
        # change flips this default, mail stops arriving on the machine's judgement and
        # nothing on any page says which sender or why.
        assert sett.noise_auto_promote is False


class TestTheBatchPathRecognises:
    """The last hole in the audit's §1a: a batch collect applied hundreds of extractions
    and triggered no disposal, no detection, no replan, no notification.

    Overnight batch mode is when the largest change to the ledger happens, so it was the
    entry point that needed the recognition loop most and had it least.
    """

    def test_a_collect_that_applied_something_runs_the_loop(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backglass import batch as batch_mod

        ran: list[str] = []

        def fake_collect(c, s_, _client=None):  # type: ignore[no-untyped-def]
            report = batch_mod.CollectReport(batches=1, extracted=12, run_id=99)
            return report

        monkeypatch.setattr(batch_mod, "_collect", fake_collect)
        monkeypatch.setattr(
            loop,
            "run",
            lambda c, s_, **kw: ran.append(str(kw.get("run_id"))) or [
                loop.Outcome("logic", loop.OK, ("disposed of 3",))
            ],
        )

        report = batch_mod.collect(conn, sett)

        assert ran == ["99"]  # the loop rows join to the run this collect wrote
        assert report.loop_lines == ["disposed of 3"]

    def test_a_collect_with_nothing_to_apply_leaves_the_loop_alone(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Nothing changed, and the thirty-minute sync owns the clock-driven passes. Two
        # callers racing to fill the same hole is not redundancy, it is two plans.
        from backglass import batch as batch_mod

        ran: list[str] = []
        monkeypatch.setattr(
            batch_mod,
            "_collect",
            lambda c, s_, _client=None: batch_mod.CollectReport(still_processing=2),
        )
        monkeypatch.setattr(loop, "run", lambda *a, **k: ran.append("go") or [])

        batch_mod.collect(conn, sett)
        assert ran == []

    def test_a_failing_pass_makes_the_collect_fail(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backglass import batch as batch_mod

        monkeypatch.setattr(
            batch_mod,
            "_collect",
            lambda c, s_, _client=None: batch_mod.CollectReport(batches=1, extracted=4),
        )
        monkeypatch.setattr(
            loop,
            "run",
            lambda *a, **k: [loop.Outcome("logic", loop.FAILED, error="OSError: disk")],
        )

        report = batch_mod.collect(conn, sett)
        assert report.exit_code == 1
        assert "loop pass 'logic' failed" in report.loop_failed[0]

    def test_the_recognition_happens_inside_the_collects_lock(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One critical section, not two. A sync slotting between the collect and the
        recognition would plan around a half-recognised board."""
        from pathlib import Path

        from backglass import batch as batch_mod
        from backglass import sync as sync_mod

        held: list[bool] = []
        key = str(Path(sett.db_path).expanduser().resolve())

        monkeypatch.setattr(
            batch_mod,
            "_collect",
            lambda c, s_, _client=None: batch_mod.CollectReport(batches=1, extracted=1),
        )
        monkeypatch.setattr(
            loop, "run", lambda *a, **k: held.append(key in sync_mod._HELD) or []
        )

        batch_mod.collect(conn, sett)
        assert held == [True]
