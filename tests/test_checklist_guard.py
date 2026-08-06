"""Checklist tick/untick against ids that are gone or retired.

Found by a full route sweep: ticking a vanished id was the single 500 in the route
table (checklist_tick's foreign key raised IntegrityError straight past the
ActionError catch), and unticking one deleted nothing and still answered
"unticked" — the same acted-on-nothing lie `_require_open` exists to prevent for
commitments. Both now refuse with a 422 the failed-write strip can show.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from backglass.config import Settings
from backglass.db import now_iso
from backglass.web.app import create_app


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


def _item(conn: sqlite3.Connection, *, active: int = 1) -> int:
    cursor = conn.execute(
        "INSERT INTO checklist_item (user_id, title, sort_order, active)"
        " VALUES (1, 'stretch', 0, ?)",
        (active,),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def test_ticking_a_real_item_works(client: TestClient, conn: sqlite3.Connection) -> None:
    item_id = _item(conn)
    response = client.post(f"/checklist/{item_id}/tick")
    assert response.status_code == 200
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM checklist_tick WHERE checklist_item_id = ?",
        (item_id,),
    ).fetchone()
    assert row["n"] == 1


def test_ticking_a_vanished_item_is_a_422_not_a_500(client: TestClient) -> None:
    response = client.post("/checklist/999999/tick")
    assert response.status_code == 422


def test_unticking_a_vanished_item_refuses_instead_of_lying(client: TestClient) -> None:
    response = client.post("/checklist/999999/untick")
    assert response.status_code == 422


def test_a_retired_item_refuses_both_ways(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    item_id = _item(conn, active=0)
    conn.execute(
        "INSERT INTO checklist_tick (checklist_item_id, local_date, ticked_at)"
        " VALUES (?, '2026-08-05', ?)",
        (item_id, now_iso()),
    )
    conn.commit()
    assert client.post(f"/checklist/{item_id}/tick").status_code == 422
    assert client.post(f"/checklist/{item_id}/untick").status_code == 422
