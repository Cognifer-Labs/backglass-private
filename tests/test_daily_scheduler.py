"""The daily scheduler, judged as a whole morning — and the questions it owes the day.

test_planner.py proves the P-rules one at a time; this file seeds one realistic Monday
and judges the morning END TO END: the plan the owner would actually see, the questions
that must exist before that plan can be trusted (a conflict, a stale obligation), and
the look-ahead — tomorrow's 8am exam is decided TODAY, by leaving room to prepare.

Doors driven, not functions: the `plan` CLI (the 05:45 job), catchup's deferred fill
(the sleeping-laptop morning), and notify's banner pass.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

import backglass.__main__ as cli
from backglass import notify
from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID
from backglass.plan import planner

PHOENIX = ZoneInfo("America/Phoenix")
MONDAY = date(2026, 8, 24)
TUESDAY = MONDAY + timedelta(days=1)


@pytest.fixture
def sett(settings: Settings) -> Settings:
    return settings.model_copy(update={
        "working_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "working_window": "08:00-18:00",
        "routines": "gym@17:00+60",
    })


@pytest.fixture(autouse=True)
def no_banners(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        notify, "_deliver", lambda t, b: shown.append((t, b)) or "osascript"
    )
    return shown


def _commitment(
    conn: sqlite3.Connection,
    what: str,
    *,
    minutes: int,
    due: str | None = None,
    rollover: int = 0,
    occurred: str = "2026-08-10T00:00:00Z",
) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, content_hash, triage_verdict)"
        " VALUES (?, 'apple-mail', ?, ?, ?, ?, ?, ?, 'keep')",
        (USER_ID, f"m-{what}", occurred, occurred, what, what, f"h-{what}"),
    )
    sid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, confidence, status,"
        " estimated_minutes, estimate_source, rollover_count, source_item_id, created_at)"
        " VALUES (?, 'i_owe', ?, ?, 0.9, 'open', ?, 'manual', ?, ?, ?)",
        (USER_ID, what, due, minutes, rollover, sid, occurred),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _class(
    conn: sqlite3.Connection, day: date, start: str, end: str, title: str
) -> None:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, raw_json, content_hash, triage_verdict)"
        " VALUES (?, 'calendar:apple', ?, ?, ?, ?, '', '{}', ?, 'keep')",
        (USER_ID, f"cal-{title}-{day}-{start}", now_iso(), f"{day}T{start}:00-07:00",
         title, f"h-{title}-{day}-{start}"),
    )
    sid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO engagement (user_id, kind, what, starts_at, ends_at,"
        " when_is_explicit, status, confidence, source_item_id, created_at)"
        " VALUES (?, 'professional', ?, ?, ?, 1, 'confirmed', 0.95, ?, ?)",
        (USER_ID, title, f"{day}T{start}:00-07:00", f"{day}T{end}:00-07:00", sid,
         now_iso()),
    )


def _a_real_monday(conn: sqlite3.Connection) -> dict[str, int]:
    """Two classes, six commitments across every priority band, dinner, gym routine."""
    _class(conn, MONDAY, "10:00", "11:00", "BIO 181")
    _class(conn, MONDAY, "13:00", "14:00", "CHM 233")
    return {
        "rollover": _commitment(conn, "finish scholarship essay", minutes=45,
                                due="2026-08-26", rollover=1),
        "overdue": _commitment(conn, "reply to housing office", minutes=60,
                               due="2026-08-17"),
        "due_today": _commitment(conn, "submit insurance form", minutes=30,
                                 due=MONDAY.isoformat()),
        "this_week": _commitment(conn, "chem problem set", minutes=90,
                                 due="2026-08-26"),
        "tiny": _commitment(conn, "email advisor back", minutes=10,
                            due="2026-08-28"),
        "huge": _commitment(conn, "digitize all freshman notes", minutes=400),
    }


def _blocks(proposal: planner.Proposal) -> list[dict]:  # type: ignore[type-arg]
    return sorted(proposal.blocks, key=lambda b: str(b["starts_at"]))


class TestOneRealMonday:
    def test_the_day_holds_together(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The integration claim: fixed life untouched, no overlaps, capacity obeyed,
        the right things first, the too-big thing said out loud."""
        ids = _a_real_monday(conn)
        proposal = planner.propose(conn, sett, MONDAY)
        blocks = _blocks(proposal)

        # Fixed life is on the plan, at its own hours, plus the gym routine.
        fixed_titles = {str(b["title"]) for b in blocks if b["kind"] in ("fixed", "routine")}
        assert {"BIO 181", "CHM 233", "Gym"} <= fixed_titles

        # No two blocks overlap — classes, gym, protected, work, small, all of it.
        spans = [
            (datetime.fromisoformat(str(b["starts_at"])),
             datetime.fromisoformat(str(b["ends_at"])), str(b["title"]))
            for b in blocks
        ]
        for i, (s1, e1, t1) in enumerate(spans):
            for s2, e2, t2 in spans[i + 1:]:
                assert e1 <= s2 or e2 <= s1, f"{t1} overlaps {t2}"

        # Work never exceeds capacity.
        assert proposal.planned_minutes <= proposal.capacity.capacity_minutes

        # P10: the rollover tops the selection order — which means it is the item
        # handed the protected deep-work block, not necessarily the 08:00 slot.
        work = [b for b in blocks if b["kind"] in ("work", "protected")]
        rollover_block = next(b for b in work if b["commitment_id"] == ids["rollover"])
        assert rollover_block["kind"] == "protected"
        # Overdue is placed; the someday-huge item is not.
        placed = {b["commitment_id"] for b in work}
        assert ids["overdue"] in placed and ids["due_today"] in placed

        # P4: the ten-minute email rides the small batch, never a block of its own.
        small = [b for b in blocks if b["kind"] == "small"]
        assert len(small) == 1
        assert all(b["commitment_id"] != ids["tiny"] for b in work)

        # The 400-minute monster is overflow, said out loud — not silently dropped.
        assert ids["huge"] in {c.commitment_id for c in proposal.overflow}

    def test_the_protected_block_prefers_the_peak_and_survives_fragmentation(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """P7 with its P6 fallback, both directions. An open morning puts deep work
        inside the 09:00–12:00 peak. The real Monday's 10:00 class fragments the peak
        below the protected minimum — and the block must then land in the longest
        slot rather than vanish, because protecting the afternoon beats protecting
        nothing."""
        ids = _a_real_monday(conn)
        fragmented = planner.propose(conn, sett, MONDAY)
        protected = [b for b in fragmented.blocks if b["kind"] == "protected"]
        assert len(protected) == 1, "a fragmented peak must not cost the deep block"
        assert fragmented.protected_placed

        # Clear the classes: the peak is whole again and deep work goes home.
        conn.execute("DELETE FROM engagement")
        del ids
        open_morning = planner.propose(conn, sett, MONDAY)
        protected = [b for b in open_morning.blocks if b["kind"] == "protected"]
        assert len(protected) == 1
        start = datetime.fromisoformat(str(protected[0]["starts_at"]))
        assert 9 <= start.hour < 12, "with the peak free, deep work belongs in it"

    def test_a_deferred_run_plans_the_hours_that_are_left(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """launchd deferred to 15:00: no block may start in the morning that already
        happened, and the afternoon still gets the most urgent work."""
        ids = _a_real_monday(conn)
        at_three = datetime(2026, 8, 24, 15, 0, tzinfo=PHOENIX)
        proposal = planner.propose(conn, sett, MONDAY, now=at_three)

        work = [b for b in proposal.blocks if b["kind"] in ("work", "small", "protected")]
        assert work, "an afternoon is not nothing"
        for b in work:
            assert datetime.fromisoformat(str(b["starts_at"])) >= at_three
        placed = {b["commitment_id"] for b in work}
        assert ids["rollover"] in placed or ids["overdue"] in placed


class TestTheMorningAsks:
    """The plan is only as good as what it knows it does not know. At day start the
    scheduler must ASK — about today's collisions, about obligations that may be
    dead — and it must look at tomorrow."""

    def _cli(self, sett: Settings, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
        monkeypatch.setattr(cli, "get_settings", lambda: sett)
        monkeypatch.setattr(cli, "_today", lambda _s: MONDAY)
        return CliRunner()

    def test_the_morning_plan_run_raises_the_days_questions(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drive the real 05:45 door. A timetable collision and a long-dead obligation
        exist in the ledger; after ONE `backglass plan`, both are open questions and
        the plan output says so — the owner is told the plan was made around unknowns."""
        _a_real_monday(conn)
        _class(conn, MONDAY, "10:30", "11:45", "LIA 101")  # collides with BIO 181
        _commitment(conn, "return library scanner", minutes=15, due="2026-07-01",
                    occurred="2026-06-20T00:00:00Z")  # overdue + silent for weeks
        conn.commit()

        result = self._cli(sett, monkeypatch).invoke(cli.app, ["plan"])
        assert result.exit_code == 0, result.output

        kinds = {str(r["kind"]) for r in conn.execute("SELECT kind FROM open_question")}
        assert "conflict" in kinds, "two classes at one hour must be asked about"
        assert "stale" in kinds, "a dead-quiet overdue must be asked about"
        assert "question(s) waiting" in result.output
        assert "/ask" in result.output

    def test_asking_is_once_however_many_mornings(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _a_real_monday(conn)
        _class(conn, MONDAY, "10:30", "11:45", "LIA 101")
        conn.commit()
        runner = self._cli(sett, monkeypatch)

        runner.invoke(cli.app, ["plan"])
        before = conn.execute("SELECT COUNT(*) AS n FROM open_question").fetchone()["n"]
        runner.invoke(cli.app, ["plan"])
        after = conn.execute("SELECT COUNT(*) AS n FROM open_question").fetchone()["n"]
        assert after == before, "a second morning must not re-ask"

    def test_the_plan_looks_at_tomorrow(
        self, conn: sqlite3.Connection, sett: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 8am exam tomorrow is decided today. The plan's notes carry it, through
        the real CLI door."""
        _a_real_monday(conn)
        _class(conn, TUESDAY, "08:00", "10:00", "BIO 181 midterm")
        _commitment(conn, "print lab worksheet", minutes=15, due=TUESDAY.isoformat())
        conn.commit()

        result = self._cli(sett, monkeypatch).invoke(cli.app, ["plan"])
        assert "Tomorrow holds: BIO 181 midterm at 08:00" in result.output
        assert '"print lab worksheet" due' in result.output
        assert "leave room to prepare" in result.output

    def test_a_fully_booked_day_still_hears_about_tomorrow(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The early-return day (no capacity) is exactly the day that needs the
        look-ahead most."""
        booked = sett.model_copy(update={"working_window": "10:00-11:00"})
        _class(conn, MONDAY, "10:00", "11:00", "BIO 181")  # eats the whole window
        _class(conn, TUESDAY, "08:00", "10:00", "BIO 181 midterm")

        proposal = planner.propose(conn, booked, MONDAY)
        assert not proposal.capacity.plannable
        assert any("Tomorrow holds" in n for n in proposal.notes)

    def test_a_quiet_tomorrow_adds_no_note(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        _a_real_monday(conn)
        proposal = planner.propose(conn, sett, MONDAY)
        assert not any("Tomorrow holds" in n for n in proposal.notes)

    def test_the_deferred_morning_gets_the_same_questions(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """The sleeping-laptop path: catchup fills the plan at 09:00 and the day's
        questions arrive with it — a deferred morning is still a morning."""
        from backglass import catchup

        _a_real_monday(conn)
        _class(conn, MONDAY, "10:30", "11:45", "LIA 101")
        at_nine = datetime(2026, 8, 24, 9, 0, tzinfo=PHOENIX)

        produced = catchup.run(conn, sett, now=at_nine)

        assert "plan" in [f.surface for f in produced]
        kinds = {str(r["kind"]) for r in conn.execute("SELECT kind FROM open_question")}
        assert "conflict" in kinds


class TestTheMorningBanner:
    def test_tomorrow_prep_is_a_banner_too(
        self, conn: sqlite3.Connection, sett: Settings,
        no_banners: list[tuple[str, str]],
    ) -> None:
        _class(conn, TUESDAY, "08:00", "10:00", "BIO 181 midterm")
        _commitment(conn, "print lab worksheet", minutes=15, due=TUESDAY.isoformat())

        at_nine = datetime(2026, 8, 24, 9, 0, tzinfo=PHOENIX)
        sent = notify.run(conn, sett, now=at_nine)

        prep = [s for s in sent if s.kind == "tomorrow-prep"]
        assert len(prep) == 1
        assert "BIO 181 midterm at 08:00" in prep[0].body
        assert '"print lab worksheet" due' in prep[0].body
        # Once per day, like every banner.
        assert notify.run(conn, sett, now=at_nine.replace(hour=10)) == []

    def test_a_quiet_tomorrow_is_a_quiet_banner(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        _commitment(conn, "someday thing", minutes=30)  # no due date, no engagement
        at_nine = datetime(2026, 8, 24, 9, 0, tzinfo=PHOENIX)
        assert [s.kind for s in notify.run(conn, sett, now=at_nine)] == []

    def test_a_proposed_plan_tomorrow_is_not_yet_an_obligation(
        self, conn: sqlite3.Connection, sett: Settings
    ) -> None:
        """"We should get dinner tomorrow?" unanswered must not banner as something
        to prepare for — a proposal is a question, not a fact (rule 2's shape)."""
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, title, body_text, content_hash, triage_verdict)"
            " VALUES (?, 'imessage', 'chat-1', ?, ?, 'chat', 'dinner?', 'h-chat-1',"
            " 'keep')",
            (USER_ID, now_iso(), now_iso()),
        )
        sid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO engagement (user_id, kind, what, starts_at, when_is_explicit,"
            " status, confidence, source_item_id, created_at)"
            " VALUES (?, 'social', 'dinner with Sam', ?, 1, 'proposed', 0.9, ?, ?)",
            (USER_ID, f"{TUESDAY}T19:00:00-07:00", sid, now_iso()),
        )
        assert planner.tomorrow_preview(conn, MONDAY) == []
