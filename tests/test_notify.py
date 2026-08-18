"""The notification ledger: once per day, inside the window, row before banner.

No test here touches osascript — `_deliver` is monkeypatched everywhere, because a
banner on the developer's own screen during a test run is a live side effect exactly
like a live API call. The quiet-hour cases run in BOTH of the owner's zones, since
owed-at-an-hour is precisely the UTC-7/+05:30 surface the lessons cover.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from backglass import notify
from backglass.config import Settings
from backglass.ledger import USER_ID

PHOENIX = ZoneInfo("America/Phoenix")
KOLKATA = ZoneInfo("Asia/Kolkata")

#: Bound at import time, BEFORE the autouse fixture swaps the module attribute — the
#: escaping test needs the real function while every other test needs the fake.
REAL_DELIVER = notify._deliver


@pytest.fixture(autouse=True)
def no_banners(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    shown: list[tuple[str, str]] = []

    def fake(title: str, body: str) -> str:
        shown.append((title, body))
        return "osascript"

    monkeypatch.setattr(notify, "_deliver", fake)
    return shown


def _at(hour: int, minute: int = 0, *, tz: ZoneInfo = PHOENIX) -> datetime:
    return datetime(2026, 8, 18, hour, minute, tzinfo=tz)


def _due_commitment(conn: sqlite3.Connection, what: str, due: str) -> None:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, content_hash, triage_verdict)"
        " VALUES (?, 'manual', ?, '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z', ?, ?,"
        " ?, 'keep')",
        (USER_ID, f"m-{what}", what, what, f"h-{what}"),
    )
    sid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, confidence, status,"
        " source_item_id, created_at)"
        " VALUES (?, 'i_owe', ?, ?, 0.9, 'open', ?, '2026-08-10T00:00:00Z')",
        (USER_ID, what, due, sid),
    )


class TestOncePerDay:
    def test_two_syncs_one_banner(
        self, conn: sqlite3.Connection, settings: Settings,
        no_banners: list[tuple[str, str]],
    ) -> None:
        _due_commitment(conn, "submit the form", "2026-08-18")

        first = notify.run(conn, settings, now=_at(9))
        second = notify.run(conn, settings, now=_at(9, 30))

        assert [s.kind for s in first] == ["overdue-today"]
        assert second == []
        assert len(no_banners) == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM notification").fetchone()["n"] == 1

    def test_tomorrow_is_a_new_slot(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _due_commitment(conn, "submit the form", "2026-08-18")
        _due_commitment(conn, "second form", "2026-08-19")
        notify.run(conn, settings, now=_at(9))

        tomorrow = datetime(2026, 8, 19, 9, 0, tzinfo=PHOENIX)
        assert [s.kind for s in notify.run(conn, settings, now=tomorrow)] == ["overdue-today"]


class TestTheQuietHours:
    @pytest.mark.parametrize("tz", [PHOENIX, KOLKATA], ids=["phoenix", "kolkata"])
    def test_before_the_window_nothing_happens_at_all(
        self, conn: sqlite3.Connection, settings: Settings, tz: ZoneInfo
    ) -> None:
        """The owed-at pattern: no banner AND no row — a 06:00 sync must not burn
        the day's dedup slot on a banner nobody saw."""
        _due_commitment(conn, "submit the form", "2026-08-18")
        assert notify.run(conn, settings, now=_at(6, tz=tz)) == []
        assert conn.execute("SELECT COUNT(*) AS n FROM notification").fetchone()["n"] == 0

    @pytest.mark.parametrize("tz", [PHOENIX, KOLKATA], ids=["phoenix", "kolkata"])
    def test_the_window_opening_delivers_what_the_quiet_sync_left(
        self, conn: sqlite3.Connection, settings: Settings, tz: ZoneInfo
    ) -> None:
        _due_commitment(conn, "submit the form", "2026-08-18")
        notify.run(conn, settings, now=_at(6, tz=tz))
        sent = notify.run(conn, settings, now=_at(8, 30, tz=tz))
        assert [s.kind for s in sent] == ["overdue-today"]

    def test_late_night_is_quiet_too(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _due_commitment(conn, "submit the form", "2026-08-18")
        assert notify.run(conn, settings, now=_at(22, 30)) == []

    def test_record_respects_the_window_for_direct_callers(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The replanner's door gets the same quiet hours as the deciders."""
        out = notify.record(
            conn, settings, kind="plan-replaced", subject_key="2026-08-18",
            title="Plan updated", body="x", now=_at(6),
        )
        assert out is None
        assert conn.execute("SELECT COUNT(*) AS n FROM notification").fetchone()["n"] == 0


class TestWhatItSays:
    def test_the_digest_names_a_few_and_counts_the_rest(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        for i in range(5):
            _due_commitment(conn, f"task {i}", "2026-08-18")
        _due_commitment(conn, "ancient favour", "2026-07-01")

        sent = notify.run(conn, settings, now=_at(9))
        digest = next(s for s in sent if s.kind == "overdue-today")
        assert digest.title == "Due today: 5"
        assert "(+2 more)" in digest.body
        assert "1 older overdue" in digest.body

    def test_a_day_with_nothing_due_is_a_day_with_no_banner(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _due_commitment(conn, "next week thing", "2026-08-25")
        assert notify.run(conn, settings, now=_at(9)) == []

    def test_overdue_alone_does_not_wake_the_owner(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Overdue-for-weeks is the stale detector's job (a question), not a daily
        banner — a notification that fires every day is one that gets turned off."""
        _due_commitment(conn, "ancient favour", "2026-07-01")
        assert notify.run(conn, settings, now=_at(9)) == []

    def test_waiting_questions_are_one_line(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        conn.execute(
            "INSERT INTO open_question (user_id, kind, subject_key, question, asked_at)"
            " VALUES (?, 'conflict', 'k', 'Which?', '2026-08-18T00:00:00Z')",
            (USER_ID,),
        )
        sent = notify.run(conn, settings, now=_at(9))
        assert [s.kind for s in sent] == ["questions-waiting"]
        assert "1 question(s) waiting" in sent[0].title

    def test_a_queue_the_owner_is_sitting_on_does_not_rebanner_daily(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Eleven long-lived questions on the live ledger must not mean a banner
        every morning forever — only something newly ASKED re-banners. A
        notification that fires every day is one that gets turned off."""
        conn.execute(
            "INSERT INTO open_question (user_id, kind, subject_key, question, asked_at)"
            " VALUES (?, 'conflict', 'k', 'Which?', '2026-08-18T00:00:00Z')",
            (USER_ID,),
        )
        assert [s.kind for s in notify.run(conn, settings, now=_at(9))] == [
            "questions-waiting"
        ]

        day2 = datetime(2026, 8, 19, 9, 0, tzinfo=PHOENIX)
        assert notify.run(conn, settings, now=day2) == []  # same queue, silence

        # A new question re-banners: asked_at after the last banner's created_at.
        conn.execute(
            "INSERT INTO open_question (user_id, kind, subject_key, question, asked_at)"
            " VALUES (?, 'stale', 'k2', 'Still real?', ?)",
            (USER_ID, datetime(2026, 8, 20, 8, 0).isoformat()),
        )
        day3 = datetime(2026, 8, 20, 9, 0, tzinfo=PHOENIX)
        sent = notify.run(conn, settings, now=day3)
        assert [s.kind for s in sent] == ["questions-waiting"]
        assert "2 question(s) waiting" in sent[0].title


class TestTheRowIsTheRecord:
    def test_a_failed_banner_still_leaves_its_row(
        self, conn: sqlite3.Connection, settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(notify, "_deliver", lambda *_a: "failed: no GUI session")
        _due_commitment(conn, "submit the form", "2026-08-18")

        sent = notify.run(conn, settings, now=_at(9))
        assert sent[0].delivered == "failed: no GUI session"
        row = conn.execute("SELECT delivered FROM notification").fetchone()
        assert row["delivered"] == "failed: no GUI session"

    def test_one_decider_down_is_not_the_surface_down(
        self, conn: sqlite3.Connection, settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def boom(*_a: object) -> None:
            raise RuntimeError("decider broke")

        monkeypatch.setattr(notify, "_due_today", boom)
        conn.execute(
            "INSERT INTO open_question (user_id, kind, subject_key, question, asked_at)"
            " VALUES (?, 'conflict', 'k', 'Which?', '2026-08-18T00:00:00Z')",
            (USER_ID,),
        )
        sent = notify.run(conn, settings, now=_at(9))
        assert [s.kind for s in sent] == ["questions-waiting"]


class TestTheEscapingDoor:
    def test_a_hostile_title_rides_argv_not_script_source(
        self, conn: sqlite3.Connection, settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The real _deliver, with subprocess.run captured: the quote-bearing title
        must arrive as an argv element, byte-identical, never inside the -e script."""
        calls: list[list[str]] = []

        class FakeProc:
            returncode = 0
            stderr = ""

        def fake_run(argv: list[str], **_kw: object) -> FakeProc:
            calls.append(list(argv))
            return FakeProc()

        import subprocess as sp

        monkeypatch.setattr(sp, "run", fake_run)
        title = 'say "rm -rf" with title "x"'
        out = REAL_DELIVER(title, "body")

        assert out == "osascript"
        (argv,) = calls
        assert argv[3] == title  # verbatim argv element
        assert title not in argv[2]  # and nowhere in the script source
        del conn, settings
