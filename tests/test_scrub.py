"""The scrub board: everything worth a decision, once, with a reason attached.

The owner, 2026-08-20: *"it is planning for things that are obviously done, for example i
already moved in on the 9th"*. The board held 366 open commitments and 64 past due against
a question surface that offers five a day, so the tests that matter here are the ones
about *coverage* — a row that qualifies and does not appear is a row that stays on the
board forever — and about the grouping, because a commitment offered twice is a second
click on a closed commitment.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from backglass import scrub
from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID
from backglass.web.app import create_app

TODAY = date(2026, 8, 20)


def a_commitment(
    conn: sqlite3.Connection,
    what: str,
    *,
    due: str | None = None,
    status: str = "open",
    source: str = "apple-mail",
    occurred_at: str = "2026-08-19T09:00:00-07:00",
    source_item_id: int | None = None,
) -> int:
    if source_item_id is None:
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, author, title, body_text, content_hash, triage_verdict)"
            " VALUES (?, ?, ?, ?, ?, 'a@b.com', 'Subj', 'body', ?, 'keep')",
            (USER_ID, source, f"x-{what}", now_iso(), occurred_at, f"h-{what}"),
        )
        source_item_id = int(
            conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        )
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, estimated_minutes,"
        " estimate_source, confidence, status, source_item_id, created_at)"
        " VALUES (?, 'i_owe', ?, ?, 30, 'manual', 0.9, ?, ?, ?)",
        (USER_ID, what, due, status, source_item_id, now_iso()),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _ids(groups: list[scrub.Group], key: str) -> list[int]:
    for group in groups:
        if group.key == key:
            return [row.id for row in group.rows]
    return []


# ── 1. what lands on the board ─────────────────────────────────────────────


class TestDetection:
    def test_an_overdue_commitment_is_offered(self, conn) -> None:  # type: ignore[no-untyped-def]
        """The move-in case. Eleven days past due, under `staleness`'s fourteen-day gate,
        so nothing else on the system would ever raise it."""
        cid = a_commitment(conn, "Move-in: Willow Hall 502", due="2026-08-09")

        assert _ids(scrub.board(conn, TODAY), "overdue") == [cid]

    def test_a_commitment_inside_the_overdue_window_is_left_alone(self, conn) -> None:  # type: ignore[no-untyped-def]
        """Under a week past due is 'has not got to it yet', which is not a decision the
        owner needs to be asked to make."""
        a_commitment(conn, "Email the advisor", due="2026-08-17")

        assert scrub.board(conn, TODAY) == []

    def test_a_future_commitment_is_never_offered(self, conn) -> None:  # type: ignore[no-untyped-def]
        a_commitment(conn, "Submit the essay", due="2026-09-30")
        assert scrub.board(conn, TODAY) == []

    def test_a_closed_commitment_is_never_offered(self, conn) -> None:  # type: ignore[no-untyped-def]
        a_commitment(conn, "Old thing", due="2026-01-01", status="dropped")
        assert scrub.board(conn, TODAY) == []

    def test_a_row_whose_twin_the_owner_already_closed_is_offered(self, conn) -> None:  # type: ignore[no-untyped-def]
        """Commitments 92 / 213 / 304 on the live ledger: one mail, three extractions, the
        first resolved by the owner and the other two still open."""
        first = a_commitment(conn, "communicate housing issue", status="done")
        item = int(
            conn.execute(
                "SELECT source_item_id AS i FROM commitment WHERE id = ?", (first,)
            ).fetchone()["i"]
        )
        again = a_commitment(conn, "communicate the housing issue", source_item_id=item)

        assert _ids(scrub.board(conn, TODAY), "resurrected") == [again]

    def test_an_open_row_with_no_closed_sibling_is_not_a_duplicate(self, conn) -> None:  # type: ignore[no-untyped-def]
        """Two open extractions of one mail are a dedupe problem, not a scrub row. The
        group's claim is specifically that the owner already decided this one."""
        first = a_commitment(conn, "thing one")
        item = int(
            conn.execute(
                "SELECT source_item_id AS i FROM commitment WHERE id = ?", (first,)
            ).fetchone()["i"]
        )
        a_commitment(conn, "thing one again", source_item_id=item)

        assert _ids(scrub.board(conn, TODAY), "resurrected") == []


# ── 2. no row is offered twice ─────────────────────────────────────────────


def test_a_row_that_qualifies_twice_appears_once(conn) -> None:  # type: ignore[no-untyped-def]
    """Overdue *and* a duplicate of something closed. Offered in both groups it would be
    acted on twice, and the second click lands on a closed commitment."""
    first = a_commitment(conn, "housing issue", status="done")
    item = int(
        conn.execute(
            "SELECT source_item_id AS i FROM commitment WHERE id = ?", (first,)
        ).fetchone()["i"]
    )
    both = a_commitment(conn, "housing issue again", due="2026-01-01", source_item_id=item)

    groups = scrub.board(conn, TODAY)
    everywhere = [row.id for group in groups for row in group.rows]

    assert everywhere.count(both) == 1
    assert _ids(groups, "resurrected") == [both]


def test_counts_agree_with_the_board(conn) -> None:  # type: ignore[no-untyped-def]
    a_commitment(conn, "one", due="2026-01-01")
    a_commitment(conn, "two", due="2026-02-01")

    counts = scrub.counts(conn, TODAY)

    assert counts["total"] == sum(
        len(group.rows) for group in scrub.board(conn, TODAY)
    )
    assert counts["total"] == 2


def test_every_row_carries_a_reason(conn) -> None:  # type: ignore[no-untyped-def]
    """CLAUDE.md rule 1 in the shape this page needs it: a row asking the owner to close
    something must say why it is asking."""
    a_commitment(conn, "one", due="2026-01-01")

    for group in scrub.board(conn, TODAY):
        assert group.blurb
        for row in group.rows:
            assert row.reason.strip()


# ── 3. the page ────────────────────────────────────────────────────────────


class TestThePage:
    @pytest.fixture
    def client(self, conn: sqlite3.Connection, settings: Settings) -> TestClient:
        del conn
        return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")

    def test_an_empty_board_says_so(self, client: TestClient) -> None:
        body = client.get("/scrub").text
        assert "Every open commitment still looks live" in body

    def test_a_row_renders_with_its_three_verdicts(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        cid = a_commitment(conn, "Move-in: Willow Hall 502", due="2026-08-09")
        conn.commit()

        body = client.get("/scrub").text

        assert "Move-in: Willow Hall 502" in body
        for verdict in ("done", "keep", "drop"):
            assert f"/scrub/{cid}/{verdict}" in body

    def test_done_resolves_the_commitment(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        cid = a_commitment(conn, "Move-in", due="2026-08-09")
        conn.commit()

        assert client.post(f"/scrub/{cid}/done").status_code == 200

        row = conn.execute(
            "SELECT status, resolution_note FROM commitment WHERE id = ?", (cid,)
        ).fetchone()
        assert row["status"] == "done"
        assert "scrub" in str(row["resolution_note"])

    def test_drop_tombstones_the_commitment(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        cid = a_commitment(conn, "Dead thing", due="2026-01-01")
        conn.commit()

        client.post(f"/scrub/{cid}/drop")

        assert conn.execute(
            "SELECT status FROM commitment WHERE id = ?", (cid,)
        ).fetchone()["status"] == "dropped"

    def test_keep_pushes_it_out_and_leaves_it_open(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """A snooze, not a resolution: the owner said this one is real, not finished."""
        cid = a_commitment(conn, "Still mine", due="2026-01-01")
        conn.commit()

        client.post(f"/scrub/{cid}/keep")

        row = conn.execute(
            "SELECT status, due_at FROM commitment WHERE id = ?", (cid,)
        ).fetchone()
        assert row["status"] == "open"
        assert str(row["due_at"])[:10] > "2026-01-01"

    def test_keep_actually_clears_the_row_from_the_board(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """The semantics the button promises. `actions.snooze` counts from the existing
        due date, so a naive thirty days on a row eight months overdue lands it still
        overdue and straight back on this page — the button would have lied."""
        cid = a_commitment(conn, "Clean fishtank", due="2026-01-06")
        conn.commit()
        assert cid in [r.id for g in scrub.board(conn, TODAY) for r in g.rows]

        client.post(f"/scrub/{cid}/keep")

        assert scrub.board(conn, TODAY) == []
        due = conn.execute(
            "SELECT due_at FROM commitment WHERE id = ?", (cid,)
        ).fetchone()["due_at"]
        assert str(due)[:10] > TODAY.isoformat()

    def test_keep_on_an_undated_row_does_not_crash(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        first = a_commitment(conn, "handled", status="done")
        item = int(
            conn.execute(
                "SELECT source_item_id AS i FROM commitment WHERE id = ?", (first,)
            ).fetchone()["i"]
        )
        cid = a_commitment(conn, "handled again", due=None, source_item_id=item)
        conn.commit()

        assert client.post(f"/scrub/{cid}/keep").status_code == 200

    def test_a_row_already_closed_elsewhere_answers_without_erroring(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """Two tabs open on a full board is a normal thing to have."""
        cid = a_commitment(conn, "Gone", due="2026-01-01", status="done")
        conn.commit()

        response = client.post(f"/scrub/{cid}/drop")

        assert response.status_code == 422
        assert "already" in response.text

    def test_an_unknown_verdict_is_refused(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        cid = a_commitment(conn, "Thing", due="2026-01-01")
        conn.commit()

        assert client.post(f"/scrub/{cid}/burn").status_code == 400
        assert conn.execute(
            "SELECT status FROM commitment WHERE id = ?", (cid,)
        ).fetchone()["status"] == "open"
