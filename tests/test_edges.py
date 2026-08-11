"""The values nobody types, and the states nobody drives.

Every test here is a defect that a green suite of 1,441 tests did not see, because each
of those tests drives a value someone chose to write down. These were found by two
adversarial sweeps — every route against an empty ledger with hostile path and query
parameters, then every write against a ledger seeded with one of everything — and they
are kept as tests so the next such value is caught by CI rather than by a sweep.

Two of them lost data: a snooze large enough to overflow SQLite's date arithmetic
erased an open commitment's deadline, and quick-add wrote whatever was typed in the due
field straight into the column the board sorts by.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from backglass import __main__ as cli
from backglass import db
from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID, Ledger, LedgerError
from backglass.web import actions
from backglass.web.app import create_app

#: Larger than SQLite's INTEGER, which is where FastAPI's unbounded `int` used to hand
#: the driver an OverflowError instead of the route handing back a 404.
TOO_BIG = 10**30


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn  # migrated on disk; the app opens its own connections
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


def _source_item(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, content_hash, triage_verdict, extraction_version)"
        " VALUES (?, 'manual', 'edge-1', ?, ?, 'a@example.com', 't', 'b', 'h', 'keep',"
        " 'manual')",
        (USER_ID, now_iso(), now_iso()),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _open_commitment(conn: sqlite3.Connection, *, due_at: str = "2026-08-10") -> int:
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, confidence, status,"
        " source_item_id, created_at) VALUES (?, 'i_owe', 'seed', ?, 0.9, 'open', ?, ?)",
        (USER_ID, due_at, _source_item(conn), now_iso()),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


class TestAMalformedDateIsAnAnswer:
    """`/brief/{on_date}` has always answered 422, because its parameter is typed
    `date`. The schedule pages parsed a string in the body and 500'd on everything
    `date.fromisoformat` cannot read — which includes a stale bookmark and a typo."""

    @pytest.mark.parametrize(
        "bad", ["2026-02-30", "not-a-date", "2026-8-5", "0000-01-01", "../../etc/passwd"]
    )
    def test_the_day_page_refuses_rather_than_raising(
        self, client: TestClient, bad: str
    ) -> None:
        assert client.get(f"/schedule?date={bad}").status_code == 422

    @pytest.mark.parametrize("bad", ["2026-02-30", "not-a-date", "9999-99-99"])
    def test_the_week_page_refuses_rather_than_raising(
        self, client: TestClient, bad: str
    ) -> None:
        assert client.get(f"/schedule/week?start={bad}").status_code == 422

    def test_a_representable_date_that_is_not_a_day_of_a_life(
        self, client: TestClient
    ) -> None:
        """`9999-12-31` parses, so it used to reach the handler — and then raised
        OverflowError inside `day_bounds`, which adds a day to find the day's end."""
        for path in ("/schedule?date=", "/schedule/week?start="):
            for absurd in ("9999-12-31", "0001-01-01"):
                assert client.get(path + absurd).status_code == 422, path + absurd

    def test_the_edges_of_the_window_still_render(self, client: TestClient) -> None:
        for path in ("/schedule?date=", "/schedule/week?start="):
            for edge in ("1900-01-01", "2200-01-01"):
                assert client.get(path + edge).status_code == 200, path + edge

    @pytest.mark.parametrize("command", ["plan", "brief", "shutdown"])
    def test_the_cli_says_which_option_is_wrong(
        self, command: str, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A traceback tells the owner they broke the program. One of the CLI's date
        options (`log --on`) said so in a sentence; the rest raised."""
        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        result = CliRunner().invoke(cli.app, [command, "--date", "2026-02-30"])
        assert result.exit_code == 1
        assert "not a date" in result.output
        assert "Traceback" not in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)


class TestASnoozeCannotEraseTheDeadline:
    """SQLite's `date(x, '+N days')` returns NULL rather than raising when N overflows
    its own arithmetic, and a NULL `due_at` is not an error anywhere in this codebase —
    it means "no deadline". So the write reported "snoozed" and the commitment lost the
    date the board sorts it by, with nothing to say so."""

    def test_a_snooze_past_the_bound_is_refused(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        cid = _open_commitment(conn)
        response = client.post(f"/commitments/{cid}/snooze/{10**15}")
        assert response.status_code == 422
        assert "not a snooze" in response.json()["detail"]
        after = conn.execute("SELECT due_at FROM commitment WHERE id = ?", (cid,)).fetchone()
        assert after["due_at"] == "2026-08-10", "the refusal wrote nothing"

    def test_an_ordinary_snooze_still_works(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        cid = _open_commitment(conn)
        assert client.post(f"/commitments/{cid}/snooze/7").status_code == 200
        after = conn.execute("SELECT due_at FROM commitment WHERE id = ?", (cid,)).fetchone()
        assert after["due_at"] == "2026-08-17"


class TestAWriteThatLosesTheRaceWithTheSync:
    """WAL lets the dashboard read while the sync writes; it does not let two writers
    overlap. The sync takes a short `BEGIN IMMEDIATE` per extracted item, so a Resolve
    clicked during one of its one-to-four-minute runs queues behind that stream — and
    Python's five-second default gave up inside the window rather than outside it.

    What that cost was not an error, it was the write: `sqlite3.OperationalError` is not
    `ActionError`, so it left the route past the only handler there was, as a 500 whose
    body is Starlette's own plain-text page. The failed-write strip parses JSON, so the
    owner got a status code for a click that could simply have been made to wait.
    """

    def _holding_the_write_lock(self, settings: Settings) -> sqlite3.Connection:
        """A second writer, exactly as the sync is one: open, then hold.

        `check_same_thread=False` because the release below happens on a timer thread
        while the request blocks on this very lock — which is the situation being
        reproduced, and the only way to reproduce it from one process.
        """
        other = sqlite3.connect(
            settings.db_path, isolation_level=None, check_same_thread=False
        )
        other.execute("BEGIN IMMEDIATE")
        other.execute("UPDATE commitment SET rollover_count = rollover_count")
        return other

    def test_it_waits_for_the_other_writer_rather_than_dropping_the_write(
        self, client: TestClient, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        cid = _open_commitment(conn)
        other = self._holding_the_write_lock(settings)
        # Released from a timer rather than after the call, because the call is what has
        # to survive the wait: with the bound at five seconds and no wait at all, this is
        # the 500 the owner saw.
        timer = threading.Timer(0.4, lambda: other.execute("ROLLBACK"))
        timer.start()
        try:
            response = client.post(f"/commitments/{cid}/snooze/7")
        finally:
            timer.join()
        assert response.status_code == 200
        after = conn.execute("SELECT due_at FROM commitment WHERE id = ?", (cid,)).fetchone()
        assert after["due_at"] == "2026-08-17", "the write waited and then landed"

    def test_a_writer_that_never_lets_go_is_a_sentence_not_a_500(
        self,
        client: TestClient,
        conn: sqlite3.Connection,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The bound makes contention rare, not impossible. What is left has to say what
        happened, in the shape `oops.js` reads — it shows `detail` verbatim and falls back
        to the status code for anything that is not JSON."""
        monkeypatch.setattr(db, "BUSY_TIMEOUT_MS", 200)
        cid = _open_commitment(conn)
        other = self._holding_the_write_lock(settings)
        try:
            response = client.post(f"/commitments/{cid}/snooze/7")
        finally:
            other.execute("ROLLBACK")
        assert response.status_code == 503, "busy is not broken"
        assert "not saved" in response.json()["detail"]
        after = conn.execute("SELECT due_at FROM commitment WHERE id = ?", (cid,)).fetchone()
        assert after["due_at"] == "2026-08-10", "and it wrote nothing on the way out"


class TestQuickAddWritesADate:
    """The extraction door resolves every date through `extract/dates.resolve_due`. The
    owner's own door put the form field into the column as typed."""

    def test_a_phrase_is_resolved_the_way_extraction_resolves_one(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        typed = {"what": "call the registrar", "due_at": "friday"}
        assert client.post("/commitments/quick-add", data=typed).status_code == 200
        stored = conn.execute(
            "SELECT due_at FROM commitment ORDER BY id DESC LIMIT 1"
        ).fetchone()["due_at"]
        assert stored is not None
        assert date.fromisoformat(str(stored)[:10]) >= date.today()

    @pytest.mark.parametrize("bad", ["not-a-date", "2026-02-30", "9999-99-99", "x" * 300])
    def test_an_unreadable_date_is_refused_rather_than_stored(
        self, client: TestClient, conn: sqlite3.Connection, bad: str
    ) -> None:
        response = client.post(
            "/commitments/quick-add", data={"what": "a thing", "due_at": bad}
        )
        assert response.status_code == 422
        assert "could not read" in response.json()["detail"]
        assert conn.execute("SELECT COUNT(*) AS n FROM commitment").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM source_item").fetchone()["n"] == 0, (
            "a refused quick-add must not leave the owner's words in the ledger with no "
            "commitment on them"
        )

    def test_no_due_date_is_still_legal(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        assert (
            client.post("/commitments/quick-add", data={"what": "someday"}).status_code == 200
        )
        assert (
            conn.execute("SELECT due_at FROM commitment").fetchone()["due_at"] is None
        )

    def test_a_commitment_is_a_line_not_a_document(self, client: TestClient) -> None:
        response = client.post("/commitments/quick-add", data={"what": "x" * 20_000})
        assert response.status_code == 422
        assert "not a document" in response.json()["detail"]

    def test_the_ledger_itself_refuses_a_date_shaped_string(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The guard is at the INSERT, not only at the door that was wrong: the next
        door has not been written yet."""
        ledger = Ledger(conn, settings)
        with pytest.raises(LedgerError, match="not a due date"):
            ledger.insert_commitment(
                direction="i_owe",
                entity_id=None,
                what="w",
                due_at="tomorrow",
                estimated_minutes=None,
                estimate_source=None,
                confidence=1.0,
                source_item_id=_source_item(conn),
            )
        assert conn.execute("SELECT COUNT(*) AS n FROM commitment").fetchone()["n"] == 0

    @pytest.mark.parametrize("good", [None, "2026-08-10", "2026-08-10T17:00:00-07:00"])
    def test_the_shapes_extraction_produces_are_accepted(
        self, conn: sqlite3.Connection, settings: Settings, good: str | None
    ) -> None:
        Ledger(conn, settings).insert_commitment(
            direction="i_owe",
            entity_id=None,
            what="w",
            due_at=good,
            estimated_minutes=None,
            estimate_source=None,
            confidence=1.0,
            source_item_id=_source_item(conn),
        )


class TestAnIdTooLargeForTheDatabase:
    """FastAPI's `int` is Python's, which is unbounded; SQLite's is 64-bit. Twenty
    routes reached the driver and died there with OverflowError."""

    @pytest.mark.parametrize(
        "path",
        [
            "/people/{id}",
            "/source/{id}",
            "/roadmaps/{id}",
            "/b/{id}.gif",
        ],
    )
    def test_a_read_answers_instead_of_raising(self, client: TestClient, path: str) -> None:
        assert client.get(path.format(id=TOO_BIG)).status_code == 422

    @pytest.mark.parametrize(
        "path",
        [
            "/commitments/{id}/resolve",
            "/commitments/{id}/drop",
            "/review/{id}/accept",
            "/review/plan/{id}/reject",
            "/checklist/{id}/tick",
            "/checklist/{id}/untick",
            "/people/{id}/edit",
            "/roadmaps/{id}/drop",
            "/chats/{id}/monitor",
            "/memory/{id}/forget",
        ],
    )
    def test_a_write_answers_instead_of_raising(self, client: TestClient, path: str) -> None:
        assert client.post(path.format(id=TOO_BIG)).status_code == 422

    def test_a_rowid_is_at_least_one(self, client: TestClient) -> None:
        """SQLite hands rowids out from 1 upward, so 0 and the negatives name nothing."""
        assert client.get("/people/0").status_code == 422
        assert client.post("/commitments/-1/resolve").status_code == 422


class TestAStaleTabIsToldWhy:
    """Every action in `web/actions.py` checks its row before writing — except the two
    that failed in opposite directions: `tick` let a FOREIGN KEY failure escape as a
    500, and `untick` deleted nothing and reported "unticked"."""

    def test_ticking_a_vanished_item_is_refused(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        conn.execute("INSERT INTO checklist_item (user_id, title) VALUES (?, 'x')", (USER_ID,))
        item_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.commit()
        assert client.post(f"/checklist/{item_id}/tick").status_code == 200

        conn.execute("DELETE FROM checklist_item WHERE id = ?", (item_id,))
        conn.commit()
        for verb in ("tick", "untick"):
            response = client.post(f"/checklist/{item_id}/{verb}")
            assert response.status_code == 422, verb
            assert "no checklist item" in response.json()["detail"], verb


class TestNumbersThatAreNotNumbers:
    """The convention of `goals/activities.MAX_HOURS_PER_ENTRY`: a bound far above any
    real entry, so it can only ever catch a mistake."""

    def test_an_estimate_longer_than_a_working_life(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        cid = _open_commitment(conn)
        response = client.post(f"/commitments/{cid}/estimate/{10**15}")
        assert response.status_code == 422
        assert "not an estimate" in response.json()["detail"]
        assert (
            conn.execute(
                "SELECT estimated_minutes FROM commitment WHERE id = ?", (cid,)
            ).fetchone()["estimated_minutes"]
            is None
        )

    def test_a_cadence_of_a_quadrillion_a_week(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO goal (user_id, title, horizon, definition_of_done, status,"
            " created_at) VALUES (?, 'g', 'annual', 'd', 'active', ?)",
            (USER_ID, now_iso()),
        )
        goal_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO target (goal_id, kind, title, weekly_count, created_at, user_id)"
            " VALUES (?, 'cadence', 't', 3, ?, ?)",
            (goal_id, now_iso(), USER_ID),
        )
        target_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.commit()

        response = client.post(f"/targets/{target_id}/weekly/{10**15}")
        assert response.status_code == 422
        assert "not a cadence" in response.json()["detail"]
        assert (
            conn.execute(
                "SELECT weekly_count FROM target WHERE id = ?", (target_id,)
            ).fetchone()["weekly_count"]
            == 3
        )

    def test_the_ordinary_values_are_untouched(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """A bound that catches a real entry is a worse bug than the one it fixed."""
        cid = _open_commitment(conn)
        assert client.post(f"/commitments/{cid}/estimate/45").status_code == 200
        assert actions.MAX_ESTIMATE_MINUTES > 60 * 24
        assert actions.MAX_SNOOZE_DAYS > 365


class TestTheDayViewShowsTheDay:
    """A confirmed 19:00 dinner rendered as an empty day.

    Two readers answered "what is fixed on this day" and disagreed: `capacity.compute`
    combined calendar events with confirmed plans, while the Schedule page called
    `fixed_events` alone. The plan blocks the page also draws come from the planner,
    which drops anything outside the working window — so nothing was going to carry an
    evening plan onto any schedule surface.
    """

    def _plan(
        self,
        conn: sqlite3.Connection,
        *,
        starts: str,
        ends: str,
        what: str = "Dinner with Sam",
        confidence: float = 0.95,
        status: str = "confirmed",
    ) -> None:
        conn.execute(
            "INSERT INTO engagement (user_id, kind, what, starts_at, ends_at,"
            " when_is_explicit, status, confidence, source_item_id, created_at)"
            " VALUES (?, 'social', ?, ?, ?, 1, ?, ?, ?, ?)",
            (USER_ID, what, starts, ends, status, confidence, _source_item(conn), now_iso()),
        )
        conn.commit()

    def test_an_evening_plan_is_on_the_day(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        self._plan(
            conn,
            starts="2026-08-10T19:00:00-07:00",
            ends="2026-08-10T21:00:00-07:00",
        )
        page = client.get("/schedule?date=2026-08-10")
        assert page.status_code == 200
        assert "Dinner with Sam" in page.text

    def test_a_plan_inside_working_hours_is_too(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """Not only the evening case: before the planner has run, an in-window plan was
        just as invisible — there was no plan_block for it yet and no reader for it."""
        self._plan(
            conn,
            starts="2026-08-10T14:00:00-07:00",
            ends="2026-08-10T15:00:00-07:00",
            what="Coffee with Dana",
        )
        assert "Coffee with Dana" in client.get("/schedule?date=2026-08-10").text

    def test_the_week_grid_shows_it_too(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        self._plan(
            conn,
            starts="2026-08-13T19:00:00-07:00",
            ends="2026-08-13T21:00:00-07:00",
        )
        assert "Dinner with Sam" in client.get("/schedule/week?start=2026-08-10").text

    def test_a_low_confidence_plan_is_not_asserted_as_a_block(
        self, client: TestClient, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Rule 2. A plan the model is unsure of is a question for the review queue, not
        a thing on the schedule — the same gate the brief and the planner apply."""
        self._plan(
            conn,
            starts="2026-08-10T19:00:00-07:00",
            ends="2026-08-10T21:00:00-07:00",
            what="Maybe drinks",
            confidence=max(0.0, settings.confidence_threshold - 0.2),
        )
        assert "Maybe drinks" not in client.get("/schedule?date=2026-08-10").text

    def test_a_declined_plan_is_not_on_the_day(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        self._plan(
            conn,
            starts="2026-08-10T19:00:00-07:00",
            ends="2026-08-10T21:00:00-07:00",
            what="Cancelled thing",
            status="declined",
        )
        assert "Cancelled thing" not in client.get("/schedule?date=2026-08-10").text

    def test_it_still_does_not_eat_the_working_day(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The window filter has to stay in `compute`, not move into the shared reader:
        an evening dinner is on the day and is not two hours off the work capacity."""
        from backglass.plan import capacity

        self._plan(
            conn,
            starts="2026-08-10T19:00:00-07:00",
            ends="2026-08-10T21:00:00-07:00",
        )
        with_plan = capacity.compute(conn, settings, date(2026, 8, 10))
        conn.execute("DELETE FROM engagement")
        conn.commit()
        without = capacity.compute(conn, settings, date(2026, 8, 10))
        # Compared against the same day without the plan rather than to zero: the
        # clamping inside `compute` already makes an out-of-window event contribute no
        # fixed minutes, so `== 0` passes with the window filter deleted. The buffer it
        # would still reserve is the part that moves, and capacity is what the owner
        # reads.
        assert with_plan.capacity_minutes == without.capacity_minutes
        # Not `== 0` any more: routines (lunch, the in-window slice of gym) legitimately
        # occupy fixed minutes on every day. The evening plan must add nothing to them.
        assert with_plan.fixed_minutes == without.fixed_minutes


class TestOneUnreadableRowIsNotABlankPage:
    """`plan_block.starts_at` is TEXT with no CHECK behind it, and the page slices the
    clock out of it by position. One row that is not a full ISO timestamp raised
    `ValueError: invalid literal for int()` through the template — taking the day page
    down, and the week grid's other six days with it."""

    def _block(self, conn: sqlite3.Connection, starts: str, ends: str, title: str) -> None:
        row = conn.execute(
            "SELECT id FROM day_plan WHERE local_date = ?", ("2026-08-10",)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO day_plan (user_id, local_date, tz, capacity_minutes,"
                " generated_at) VALUES (?, '2026-08-10', 'America/Phoenix', 480, ?)",
                (USER_ID, now_iso()),
            )
            row = conn.execute(
                "SELECT id FROM day_plan WHERE local_date = ?", ("2026-08-10",)
            ).fetchone()
        conn.execute(
            "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, title, user_id)"
            " VALUES (?, ?, ?, 'work', ?, ?)",
            (row["id"], starts, ends, title, USER_ID),
        )
        conn.commit()

    @pytest.mark.parametrize("stamp", ["09:00", "not-a-timestamp", "", "2026-08-10"])
    def test_the_page_still_renders(
        self, client: TestClient, conn: sqlite3.Connection, stamp: str
    ) -> None:
        self._block(conn, stamp, stamp, "unreadable")
        self._block(
            conn, "2026-08-10T14:00:00-07:00", "2026-08-10T15:00:00-07:00", "readable one"
        )
        page = client.get("/schedule?date=2026-08-10")
        assert page.status_code == 200
        assert "readable one" in page.text, "the good row must survive the bad one"
        assert client.get("/schedule/week?start=2026-08-10").status_code == 200

    @pytest.mark.parametrize("stamp", ["09:00", "not-a-timestamp"])
    def test_it_is_dropped_rather_than_placed_at_midnight(
        self, client: TestClient, conn: sqlite3.Connection, stamp: str
    ) -> None:
        """The tempting fallback is 00:00, and it is worse than dropping the row: a
        schedule that asserts an hour nothing says is worse than one missing a row it
        could not read."""
        self._block(conn, stamp, stamp, "unreadable")
        assert "unreadable" not in client.get("/schedule?date=2026-08-10").text


class TestTheKeyboardSurvivesAWrite:
    """Every write on the dashboard swaps a whole panel, which discards the DOM the
    keyboard selection lived on. `index` is module state and survives; the mark and
    the focus did not — so a second `x` acted on a commitment that was never selected
    and could not be seen to be selected. Verified in a browser: three presses of `x`
    used to leave `data-selected` null and `document.activeElement` on <body>, and now
    walk the board with the mark visible at each step.
    """

    DASHBOARD = (
        Path(__file__).resolve().parents[1]
        / "backglass" / "web" / "templates" / "dashboard.html"
    )

    def test_the_selection_is_restored_after_a_swap(self) -> None:
        js = self.DASHBOARD.read_text()
        handler = re.search(
            r"addEventListener\('htmx:afterSwap', \(\) => \{(.+?)\}\);", js, re.S
        )
        assert handler, "no afterSwap handler in the keyboard block"
        body = handler.group(1)
        assert "select(index)" in body, "the mark is not re-applied after a swap"
        assert "index < 0" in body, (
            "restoring unconditionally would pull focus out from under a mouse click"
        )

    def test_the_review_jump_honours_reduced_motion(self) -> None:
        js = self.DASHBOARD.read_text()
        assert "prefers-reduced-motion" in js
        assert "'smooth'" in js and "'auto'" in js, (
            "the reduced-motion branch must still scroll, just without the animation"
        )


class TestTheAccessibilityTree:
    """An axe-core pass over all twelve routes in both themes found four violation
    classes; these pin the three that were fixed. The audit needs a browser and stays
    out of CI — what is checkable here is that the specific decisions hold.
    """

    ROOT = Path(__file__).resolve().parents[1]

    def test_every_page_has_a_main_landmark(self) -> None:
        """Without it, every panel on every page sits outside a landmark and a screen
        reader has no structure to move by — one element fixed two axe violations
        across all twenty-four page/theme combinations."""
        base = (self.ROOT / "backglass" / "web" / "templates" / "base.html").read_text()
        assert "<main" in base and "</main>" in base
        assert base.index("<main") < base.index("{% block content %}")

    def test_muted_text_on_a_wash_steps_up_the_ramp(self) -> None:
        """design-system.md §2 quotes the ramp against paper and calls 500 "muted".
        A wash is darker than paper: 500 measures 3.74 on the due-today wash and 3.3
        on the cobalt one, both under the AA floor. The wash pair carries 700."""
        tokens = (self.ROOT / "design" / "tokens.css").read_text()
        assert re.search(r"--wash-fg-2:\s*var\(--ink-2\)", tokens), (
            "the wash text pair is back on the muted step, which fails AA on every "
            "washed surface in light mode"
        )

    def test_wash_containers_read_from_the_wash_pair(self) -> None:
        """tokens.css states the contract — "containers styled with the wash tokens
        set color from these, never inherit" — and the day timeline's event blocks
        were the ones that never did."""
        css = (self.ROOT / "backglass" / "web" / "static" / "dashboard.css").read_text()
        block = css[css.index(".tl .ev{") : css.index(".tl .ev.slim")]
        assert "var(--ink-muted)" not in block, (
            "an event block wears an ink wash; its metadata cannot use the paper-rated "
            "muted step"
        )


class TestTheKeyboardLegendMatchesTheKeyboard:
    """A legend is a claim about the program, and this one had drifted: base.html
    binds 1-8 to the eight sidebar destinations, the dashboard advertised 1-6, and
    the two keys that reach Chats and Brief were undiscoverable.

    Derived from the bindings rather than restated. A test that hard-codes "1-8"
    teaches the next author to edit the assertion instead of reading it — the same
    shape as the migration-version lists tasks/lessons.md already regrets.
    """

    TEMPLATES = Path(__file__).resolve().parents[1] / "backglass" / "web" / "templates"

    def _bound_keys(self) -> list[str]:
        base = (self.TEMPLATES / "base.html").read_text()
        table = re.search(r"const pages = \{(.+?)\}", base, re.S)
        assert table, "base.html no longer declares a `pages` map"
        return re.findall(r"'(\d)':", table.group(1))

    def test_every_bound_page_key_is_advertised(self) -> None:
        keys = self._bound_keys()
        assert keys, "no page keys found"
        legend = (self.TEMPLATES / "dashboard.html").read_text()
        span = f'<span class="kbd">{keys[0]}</span>-<span class="kbd">{keys[-1]}</span>'
        span = span.replace("-", "\u2013")
        assert span in legend, (
            f"the legend does not cover {keys[0]}-{keys[-1]}; the bindings did not "
            "change, the sentence describing them did"
        )

    def test_the_keys_are_contiguous(self) -> None:
        """A range in the legend only tells the truth if the map has no holes."""
        keys = [int(k) for k in self._bound_keys()]
        assert keys == list(range(keys[0], keys[0] + len(keys))), keys


class TestNothingIsWiderThanThePhone:
    """The dashboard measured 642px wide inside a 390px viewport, and the right
    third of every panel was unreachable on a phone.

    The measurement itself needs a browser and lives outside CI (a Playwright pass
    over every route at 390/430/768/1024/1440). What is checkable here is the
    mechanism that caused it, which was the same in all four places: a hard pixel
    floor on something inside a grid or flex track. A grid item's automatic minimum
    is its min-content width, so one un-shrinkable child silently widens its track,
    then the page, then everything in it.
    """

    TEMPLATES = Path(__file__).resolve().parents[1] / "backglass" / "web" / "templates"

    def test_no_template_hard_codes_a_width(self) -> None:
        """A width in a style attribute cannot carry a media query or a min-width:0
        escape hatch, so it is the one place a floor can never be relaxed. The
        quick-add field's `style="min-width:220px"` pushed the whole dashboard
        200px off the right of the screen."""
        offenders = [
            f"{path.name}: {line.strip()[:90]}"
            for path in sorted(self.TEMPLATES.glob("*.html"))
            for line in path.read_text().splitlines()
            if re.search(r'style="[^"]*(?:min-)?width:\s*\d', line)
        ]
        assert not offenders, (
            "widths belong in dashboard.css, where a breakpoint can reach them:\n"
            + "\n".join(offenders)
        )

    def test_the_panel_grid_cannot_be_stretched_by_its_contents(self) -> None:
        css = (
            Path(__file__).resolve().parents[1]
            / "backglass" / "web" / "static" / "dashboard.css"
        ).read_text()
        assert "grid-template-columns:repeat(2,minmax(0,1fr))" in css
        assert re.search(r"\.panel\{min-width:0", css), (
            "without min-width:0 a single wide row re-widens every panel on the page"
        )

    def test_the_board_can_be_acted_on_without_a_pointer(self) -> None:
        """Resolve, Snooze and Drop are revealed by hover, and hover is a thing a
        phone does not have — so the board, which docs/06 opens by insisting must not
        be read-only, was exactly that on a touch device. Verified in a real touch
        context: the actions resolve to display:flex and a tap posts /resolve."""
        css = (
            Path(__file__).resolve().parents[1]
            / "backglass" / "web" / "static" / "dashboard.css"
        ).read_text()
        assert "@media(hover:none){.card .acts{display:flex}}" in css

    def test_the_week_grids_day_boxes_are_tappable(self) -> None:
        """WCAG 2.5.8 asks for 24px. The day box is a 16px mark on purpose — seven
        columns plus a streak column is already the widest thing on the page — so the
        target is grown under it instead of the drawing being grown. Verified by
        clicking 5px above the square and watching the tick post; without the rule the
        same click posts nothing."""
        css = (
            Path(__file__).resolve().parents[1]
            / "backglass" / "web" / "static" / "dashboard.css"
        ).read_text()
        rule = css[css.index(".wkg button.box") :][:300]
        assert "::after" in rule and "26px" in rule

    def test_the_alerts_block_survives_the_narrow_breakpoint(self) -> None:
        """Goals and Roadmaps yield on a phone — both are summaries of a page one
        tap away. Alerts have no page of their own, so hiding them meant a failing
        source was invisible on the device the dashboard is most read on."""
        css = (
            Path(__file__).resolve().parents[1]
            / "backglass" / "web" / "static" / "dashboard.css"
        ).read_text()
        start = css.index("@media(max-width:900px)")
        depth, end = 0, start
        for i in range(start, len(css)):
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        narrow = css[start:end]
        assert "#side-alerts" in narrow, "the alerts block has no rule at this breakpoint"
        assert "#side-goals" in narrow and "#side-roadmaps" in narrow
        assert ".sblock{display:none}" not in narrow, (
            "a blanket hide takes the alerts with it — name the two blocks that yield"
        )
