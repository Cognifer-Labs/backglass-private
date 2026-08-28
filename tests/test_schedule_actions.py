"""The Schedule page stops being read-only. Owner's ask, 2026-08-27.

    "chem lab is no longer 6 to 750 thursday, why hasnt backglass backend updated to
     show that" … "everything is schedule should be clickable and interactable for user"

Two sentences, one request. The lab moved on the 23rd and the ledger knew within hours —
two `fact` rows read off MyASU — while the day page went on drawing a 6:00–7:50 pm block,
because the planner reads calendar rows and the only door to a wrong calendar row was a
Python script. `retraction.py` could not open it either: it infers a retraction only from
a window a connector re-read and certified, and the rows behind that lab are a one-shot
registrar import that no connector re-reads.

So these tests are about one property — an event drawn on the canvas can be acted on from
the canvas — plus the two ways that property can be faked: a button on something with no
row behind it, and an action that cannot be undone.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID
from backglass.web import actions
from backglass.web.app import create_app
from tests.conftest import panel_slice

#: Far enough out that nothing else the fixtures write lands on it.
DAY = date.today() + timedelta(days=3)


@pytest.fixture
def client(conn, settings: Settings):  # type: ignore[no-untyped-def]
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


def _calendar_event(conn, title: str, start_hhmm: str, end_hhmm: str) -> int:  # type: ignore[no-untyped-def]
    """One `calendar:asu` row in the shape the real registrar import wrote."""
    starts_at = f"{DAY.isoformat()}T{start_hhmm}:00-07:00"
    ends_at = f"{DAY.isoformat()}T{end_hhmm}:00-07:00"
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, raw_json, content_hash, triage_verdict) "
        "VALUES (?, 'calendar:asu', ?, ?, ?, ?, ?, ?, ?, 'drop')",
        (
            USER_ID,
            f"cal-{title}-{start_hhmm}",
            now_iso(),
            starts_at,
            title,
            title,
            f'{{"starts_at": "{starts_at}", "ends_at": "{ends_at}", '
            f'"status": "confirmed", "location": "Tempe PSD 232"}}',
            f"hash-{title}-{start_hhmm}",
        ),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _timeline(client: TestClient) -> str:
    page = client.get(f"/schedule?date={DAY.isoformat()}")
    assert page.status_code == 200
    return panel_slice(page.text, "panel-timeline")


# ── the event is reachable from the canvas ────────────────────────────────


def test_a_calendar_event_carries_its_evidence_and_its_correction(conn, client) -> None:  # type: ignore[no-untyped-def]
    """Rule 1 on the canvas: the source is one click from the block drawn out of it,
    and beside it the door that says the world moved on."""
    source_id = _calendar_event(conn, "CHM 113 (Lab)", "18:00", "19:50")

    canvas = _timeline(client)

    assert f'href="/source/{source_id}"' in canvas
    assert f"/source/{source_id}/retract" in canvas


def test_a_routine_gets_no_buttons_because_it_has_no_row(conn, client, settings) -> None:  # type: ignore[no-untyped-def]
    """The negative half, and the one that keeps the rest trustworthy.

    Routines come from configuration, not from the ledger. A button posting to a row
    that does not exist answers 422, and one dead button on a canvas of live ones is
    what teaches the owner to stop pressing any of them.
    """
    canvas = _timeline(client)

    assert "/source/None/" not in canvas
    assert "/blocks/None/" not in canvas


# ── acting on it ──────────────────────────────────────────────────────────


def test_not_happening_takes_the_event_off_the_day(conn, client) -> None:  # type: ignore[no-untyped-def]
    source_id = _calendar_event(conn, "CHM 113 (Lab)", "18:00", "19:50")
    assert "CHM 113 (Lab)" in _timeline(client)

    swapped = client.post(f"/schedule/{DAY.isoformat()}/source/{source_id}/retract")

    assert swapped.status_code == 200
    assert "CHM 113 (Lab)" not in swapped.text.split("Removed")[-1].split("Undo")[-1]
    assert "CHM 113 (Lab)" not in _timeline(client)


def test_the_answer_carries_the_way_back(conn, client) -> None:  # type: ignore[no-untyped-def]
    """A retracted event leaves the canvas, so the undo cannot live on the block it
    undoes. It is named in the strip above the ruler or it is unreachable."""
    source_id = _calendar_event(conn, "CHM 113 (Lab)", "18:00", "19:50")

    swapped = client.post(f"/schedule/{DAY.isoformat()}/source/{source_id}/retract")

    assert "CHM 113 (Lab)" in swapped.text
    assert f"/source/{source_id}/restore" in swapped.text

    restored = client.post(f"/schedule/{DAY.isoformat()}/source/{source_id}/restore")
    assert restored.status_code == 200
    assert "CHM 113 (Lab)" in _timeline(client)


def test_two_clicks_are_one_retraction(conn, client) -> None:  # type: ignore[no-untyped-def]
    """A slow connection is not an error the owner should have to read."""
    source_id = _calendar_event(conn, "CHM 113 (Lab)", "18:00", "19:50")

    client.post(f"/schedule/{DAY.isoformat()}/source/{source_id}/retract")
    again = client.post(f"/schedule/{DAY.isoformat()}/source/{source_id}/retract")

    assert again.status_code == 200
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM source_item_retraction WHERE source_item_id = ?",
        (source_id,),
    ).fetchone()
    assert rows["n"] == 1


def test_the_reason_says_it_was_the_owner_and_not_a_certified_read(conn) -> None:  # type: ignore[no-untyped-def]
    """Both are retractions; only one of them is evidence about what upstream holds.

    `retraction.py`'s safety property is that it never infers from anything but a read a
    connector certified complete. This action has no such certificate — it has the owner
    — and a reason string that did not say so would let a later reader take one for the
    other.
    """
    source_id = _calendar_event(conn, "CHM 113 (Lab)", "18:00", "19:50")

    actions.retract_source_item(conn, source_id)

    reason = conn.execute(
        "SELECT reason FROM source_item_retraction WHERE source_item_id = ?",
        (source_id,),
    ).fetchone()["reason"]
    assert reason.startswith(actions.OWNER_RETRACTION_PREFIX)
    assert "no longer returns this item" not in reason


def test_undo_refuses_to_reverse_a_connector(conn) -> None:  # type: ignore[no-untyped-def]
    """A certified-read retraction is a connector's statement that the item is gone
    upstream. Deleting it would put the row back until the next sync retracted it
    again — an undo that undoes itself is worse than no button."""
    source_id = _calendar_event(conn, "CHM 113 (Lab)", "18:00", "19:50")
    conn.execute(
        "INSERT INTO source_item_retraction "
        "(source_item_id, user_id, retracted_at, reason) VALUES (?, ?, ?, ?)",
        (source_id, USER_ID, now_iso(), "calendar:apple re-read … and no longer returns"),
    )
    conn.commit()

    with pytest.raises(actions.ActionError):
        actions.restore_source_item(conn, source_id)


def test_retracting_something_that_is_not_there_says_so(conn) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(actions.ActionError):
        actions.retract_source_item(conn, 9_999_999)


# ── the planner reads the same correction ─────────────────────────────────


def test_the_capacity_model_stops_subtracting_a_retracted_event(conn) -> None:  # type: ignore[no-untyped-def]
    """The point of the button, and the reason it is not a display filter.

    An event taken off the day has to stop costing the owner capacity, or the canvas and
    the plan disagree — which is exactly the state the lab was in for four days.
    """
    from backglass.plan import capacity

    source_id = _calendar_event(conn, "CHM 113 (Lab)", "18:00", "19:50")
    before = capacity.fixed_events(conn, DAY, "America/Phoenix")
    assert any(e.title == "CHM 113 (Lab)" for e in before)

    actions.retract_source_item(conn, source_id)

    after = capacity.fixed_events(conn, DAY, "America/Phoenix")
    assert not any(e.title == "CHM 113 (Lab)" for e in after)


def test_the_event_itself_is_never_touched(conn) -> None:  # type: ignore[no-untyped-def]
    """`source_item` is immutable and the row stays true forever: the lab really did
    meet at six until the section swap. What changes is what the day renders."""
    source_id = _calendar_event(conn, "CHM 113 (Lab)", "18:00", "19:50")

    actions.retract_source_item(conn, source_id)

    row = conn.execute(
        "SELECT title, occurred_at FROM source_item WHERE id = ?", (source_id,)
    ).fetchone()
    assert row["title"] == "CHM 113 (Lab)"
    assert row["occurred_at"].endswith("T18:00:00-07:00")


# ── the plan block half ───────────────────────────────────────────────────


def _planned_block(conn, title: str, start_hhmm: str, end_hhmm: str) -> int:  # type: ignore[no-untyped-def]
    conn.execute(
        "INSERT INTO day_plan (user_id, local_date, tz, capacity_minutes, "
        " planned_minutes, overflow_count, generated_at, status) "
        "VALUES (?, ?, 'America/Phoenix', 480, 60, 0, ?, 'proposed')",
        (USER_ID, DAY.isoformat(), now_iso()),
    )
    plan_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO plan_block (day_plan_id, user_id, starts_at, ends_at, kind, title) "
        "VALUES (?, ?, ?, ?, 'work', ?)",
        (
            plan_id,
            USER_ID,
            f"{DAY.isoformat()}T{start_hhmm}:00-07:00",
            f"{DAY.isoformat()}T{end_hhmm}:00-07:00",
            title,
        ),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def test_a_plan_block_wears_the_three_actions_it_can_honour(conn, client) -> None:  # type: ignore[no-untyped-def]
    block_id = _planned_block(conn, "Draft the reply", "14:00", "15:00")

    canvas = _timeline(client)

    assert f"/blocks/{block_id}/outcome/done" in canvas
    assert f"/blocks/{block_id}/outcome/rolled" in canvas
    assert f"/blocks/{block_id}/pin/1" in canvas


def test_done_lands_on_the_block_and_answers_with_the_canvas(conn, client) -> None:  # type: ignore[no-untyped-def]
    """The swap returns the timeline, not the Today panel. Reusing the flat /blocks/…
    routes would answer a schedule-page request with a dashboard fragment, and htmx
    would happily paste it into the canvas."""
    block_id = _planned_block(conn, "Draft the reply", "14:00", "15:00")

    swapped = client.post(
        f"/schedule/{DAY.isoformat()}/blocks/{block_id}/outcome/done"
    )

    assert swapped.status_code == 200
    assert 'id="panel-timeline"' in swapped.text
    outcome = conn.execute(
        "SELECT outcome FROM plan_block WHERE id = ?", (block_id,)
    ).fetchone()["outcome"]
    assert outcome == "done"


def test_pin_holds_the_slot_against_the_replanner(conn, client) -> None:  # type: ignore[no-untyped-def]
    block_id = _planned_block(conn, "Draft the reply", "14:00", "15:00")

    client.post(f"/schedule/{DAY.isoformat()}/blocks/{block_id}/pin/1")

    pinned = conn.execute(
        "SELECT pinned FROM plan_block WHERE id = ?", (block_id,)
    ).fetchone()["pinned"]
    assert pinned == 1


def test_an_id_that_is_not_a_block_gets_a_sentence_not_a_500(client) -> None:  # type: ignore[no-untyped-def]
    answer = client.post(f"/schedule/{DAY.isoformat()}/blocks/9999999/pin/1")
    assert answer.status_code == 422


def test_pin_is_a_toggle_and_not_a_one_way_trip(conn, client) -> None:  # type: ignore[no-untyped-def]
    """The Today panel has always drawn Pin/Unpin. A canvas that can only pin is a
    canvas where an owner who misclicks has to open a terminal to undo it — which is
    the shape of the whole complaint this page is answering."""
    block_id = _planned_block(conn, "Draft the reply", "14:00", "15:00")
    assert f"/blocks/{block_id}/pin/1" in _timeline(client)

    client.post(f"/schedule/{DAY.isoformat()}/blocks/{block_id}/pin/1")

    canvas = _timeline(client)
    assert f"/blocks/{block_id}/pin/0" in canvas
    assert "Unpin" in canvas


# ── the correction has to reach both copies of one event ──────────────────


def _planned_mirror_of(conn, title: str, start_hhmm: str, end_hhmm: str) -> int:  # type: ignore[no-untyped-def]
    """What `plan/planner` writes for every event in `cap.fixed`: a second copy of the
    calendar event, as a `kind='fixed'` plan block."""
    conn.execute(
        "INSERT INTO day_plan (user_id, local_date, tz, capacity_minutes, "
        " planned_minutes, overflow_count, generated_at, status) "
        "VALUES (?, ?, 'America/Phoenix', 480, 110, 0, ?, 'accepted')",
        (USER_ID, DAY.isoformat(), now_iso()),
    )
    plan_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO plan_block (day_plan_id, user_id, starts_at, ends_at, kind, title) "
        "VALUES (?, ?, ?, ?, 'fixed', ?)",
        (
            plan_id,
            USER_ID,
            f"{DAY.isoformat()}T{start_hhmm}:00-07:00",
            f"{DAY.isoformat()}T{end_hhmm}:00-07:00",
            title,
        ),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def test_not_happening_reaches_the_planners_copy_too(conn, client) -> None:  # type: ignore[no-untyped-def]
    """The failure this whole page exists to prevent, in miniature.

    The event is on the day twice — the calendar row and the block the planner projected
    from it. Retracting only the calendar row leaves the block drawing the same hour at
    the same minute, so the canvas is unchanged and the button reads as broken. That is a
    wrong class time surviving a correction, which is exactly the complaint.
    """
    source_id = _calendar_event(conn, "CHM 113 (Lab)", "18:00", "19:50")
    block_id = _planned_mirror_of(conn, "CHM 113 (Lab)", "18:00", "19:50")

    swapped = client.post(f"/schedule/{DAY.isoformat()}/source/{source_id}/retract")

    assert swapped.status_code == 200
    outcome = conn.execute(
        "SELECT outcome FROM plan_block WHERE id = ?", (block_id,)
    ).fetchone()["outcome"]
    assert outcome == "dropped"
    assert "CHM 113 (Lab)" not in _timeline(client), "the hour is off the canvas"


def test_undo_puts_both_copies_back(conn, client) -> None:  # type: ignore[no-untyped-def]
    source_id = _calendar_event(conn, "CHM 113 (Lab)", "18:00", "19:50")
    block_id = _planned_mirror_of(conn, "CHM 113 (Lab)", "18:00", "19:50")
    client.post(f"/schedule/{DAY.isoformat()}/source/{source_id}/retract")

    restored = client.post(
        f"/schedule/{DAY.isoformat()}/source/{source_id}/restore?block={block_id}"
    )

    assert restored.status_code == 200
    outcome = conn.execute(
        "SELECT outcome FROM plan_block WHERE id = ?", (block_id,)
    ).fetchone()["outcome"]
    assert outcome == "pending"
    assert "CHM 113 (Lab)" in _timeline(client)


def test_the_undo_strip_names_the_block_it_has_to_put_back(conn, client) -> None:  # type: ignore[no-untyped-def]
    """The undo is the only thing that knows both halves were taken. If the strip does
    not carry the block id, undo restores the calendar row and leaves the day short."""
    source_id = _calendar_event(conn, "CHM 113 (Lab)", "18:00", "19:50")
    block_id = _planned_mirror_of(conn, "CHM 113 (Lab)", "18:00", "19:50")

    swapped = client.post(f"/schedule/{DAY.isoformat()}/source/{source_id}/retract")

    assert f"/source/{source_id}/restore?block={block_id}" in swapped.text


def test_a_dropped_block_stops_holding_a_slot(conn, client) -> None:  # type: ignore[no-untyped-def]
    """`set_block_outcome` calls `dropped` "not this slot". A block still drawn is still
    holding one, and `gaps` would go on reporting the hour as busy."""
    block_id = _planned_block(conn, "Draft the reply", "14:00", "15:00")
    assert "Draft the reply" in _timeline(client)

    actions.set_block_outcome(conn, block_id, "dropped")
    conn.commit()

    assert "Draft the reply" not in _timeline(client)


def test_done_and_rolled_still_draw(conn, client) -> None:  # type: ignore[no-untyped-def]
    """The day is also a record of itself. Only "not this slot" leaves the canvas."""
    block_id = _planned_block(conn, "Draft the reply", "14:00", "15:00")

    actions.set_block_outcome(conn, block_id, "rolled")
    conn.commit()

    assert "Draft the reply" in _timeline(client)


# ── the runway panel ──────────────────────────────────────────────────────


def test_the_day_page_does_not_pay_for_the_fortnight(conn, client) -> None:  # type: ignore[no-untyped-def]
    """`runway.allocate` walks fourteen days of capacity and measured 0.58s on the
    owner's ledger. The Schedule page is opened every morning; it must not carry that
    cost to show today. The fold asks for the allocation on first open and never again."""
    page = client.get(f"/schedule?date={DAY.isoformat()}")

    panel = panel_slice(page.text, "panel-runway")
    assert 'hx-trigger="toggle once"' in panel
    assert "/schedule/runway?date=" in panel


def test_the_runway_names_a_day_for_each_obligation(conn, client, settings) -> None:  # type: ignore[no-untyped-def]
    from tests.test_planner import add_commitment

    add_commitment(conn, settings, "write the essay", minutes=60,
                   due=DAY + timedelta(days=3))
    conn.commit()

    fragment = client.get(f"/schedule/runway?date={DAY.isoformat()}")

    assert fragment.status_code == 200
    assert "write the essay" in fragment.text
    assert "sitting" in fragment.text


def test_work_that_cannot_fit_leads_the_runway(conn, client, settings) -> None:  # type: ignore[no-untyped-def]
    """It is the only part of the list that asks for a decision. Everything under it is
    the planner working."""
    from tests.test_planner import add_commitment

    add_commitment(conn, settings, "fits fine", minutes=30,
                   due=DAY + timedelta(days=5), n=1)
    add_commitment(conn, settings, "cannot possibly fit", minutes=6_000,
                   due=DAY + timedelta(days=2), n=2, estimate_source="analyzed")
    conn.commit()

    body = client.get(f"/schedule/runway?date={DAY.isoformat()}").text

    assert body.index("cannot possibly fit") < body.index("fits fine")
    assert "move the date" in body


def test_an_empty_runway_says_so_rather_than_rendering_nothing(conn, client) -> None:  # type: ignore[no-untyped-def]
    body = client.get(f"/schedule/runway?date={DAY.isoformat()}").text

    assert "Nothing to allocate" in body


def test_the_panel_is_the_whole_board_and_not_a_window_on_it(conn, client, settings) -> None:  # type: ignore[no-untyped-def]
    """The concern this began as, answered a different way.

    It was written when the panel walked a fortnight: it drew 137 obligations out of 267
    and said nothing about the other 130, so it read as complete while two thirds of the
    coursework sat outside it. The first fix was to count what fell outside and say so.
    The better one is not to have an outside — the panel now walks to the last deadline
    on the board, so a November exam gets days like everything else and the "has no day"
    line correctly has nothing to report.
    """
    from tests.test_planner import add_commitment

    add_commitment(conn, settings, "November exam", minutes=4_000, n=1,
                   due=DAY + timedelta(days=70), estimate_source="analyzed")
    conn.commit()

    body = client.get(f"/schedule/runway?date={DAY.isoformat()}").text

    assert "November exam" in body, "work due in ten weeks is outside the panel again"
    assert "The board clears on" in body
    assert "obligation(s) has no day" not in body


def test_a_runway_that_reached_everything_says_nothing_extra(conn, client, settings) -> None:  # type: ignore[no-untyped-def]
    from tests.test_planner import add_commitment

    add_commitment(conn, settings, "write the essay", minutes=60,
                   due=DAY + timedelta(days=3))
    conn.commit()

    body = client.get(f"/schedule/runway?date={DAY.isoformat()}").text

    assert "obligation(s) has no day" not in body


# ── the Now line moves ────────────────────────────────────────────────────


def test_the_day_canvas_carries_what_the_clock_needs(conn, client) -> None:  # type: ignore[no-untyped-def]
    """The Now line was drawn once, on the server, at the moment the page was built — so
    a page opened at 8am still said "now" at 8am at one in the afternoon. `now.js` moves
    it, and it can only agree with the ruler beside it if it reads the ruler's own
    window: `hours[0] * 60` is not it, because `_window` is not hour-aligned."""
    today_ = date.today()
    page = client.get(f"/schedule?date={today_.isoformat()}")

    panel = panel_slice(page.text, "panel-timeline")
    assert 'data-now-px="1"' in panel
    assert "data-now-start=" in panel
    assert f'data-now-day="{today_.isoformat()}"' in panel
    assert "/static/now.js" in page.text


def test_a_day_that_is_not_today_has_no_clock_on_it(conn, client) -> None:  # type: ignore[no-untyped-def]
    """`now_top` is None both when the day is not today and when the clock is outside
    the drawn window, and only the second one starts moving later in the day."""
    page = client.get(f"/schedule?date={DAY.isoformat()}")

    panel = panel_slice(page.text, "panel-timeline")
    assert "data-now-tz=" not in panel
    assert 'class="now"' not in panel


# ── the runway says what it counted (tasks/display-audit-2026-08-27.md) ────


def test_the_headline_cannot_claim_more_than_the_board_holds(conn, client, settings) -> None:  # type: ignore[no-untyped-def]
    """The audit's finding A, as a test.

    The panel opened with *"241 obligations, every one of them with a day"* and then drew
    forty-four rows chipped "Won't fit". `items` was `len(sittings)`, which counts an
    obligation the walk placed *partially* and knows nothing at all about the thirty-nine
    it never placed. Both halves came from one `horizon` and neither knew about the other.

    The 2026-08-27 lesson, at the display layer: count the inputs against the outputs.
    Here that means the headline's own numbers have to add up to the board.
    """
    from tests.test_planner import add_commitment

    add_commitment(conn, settings, "fits fine", minutes=30, due=DAY + timedelta(days=5), n=1)
    add_commitment(conn, settings, "cannot possibly fit", minutes=6_000,
                   due=DAY + timedelta(days=2), n=2, estimate_source="analyzed")
    conn.commit()

    body = client.get(f"/schedule/runway?date={DAY.isoformat()}").text

    finishes, total = re.search(r"for\s+(\d+) of (\d+) obligations", body).groups()
    wont = re.search(r"(\d+) do not finish before they are due", body).group(1)
    assert int(finishes) + int(wont) == int(total), (
        "the headline's own arithmetic has to close"
    )
    assert int(wont) >= 1
    # The completeness claim is gone precisely when it is false.
    assert "every one of them with a day" not in body

    # The part-placed clause is the half that carries the actual reconciliation: a
    # divisible obligation the walk starts and cannot finish is in `sittings` AND in
    # `unreachable`, and it is the five of those that the old sentence counted as
    # finished. `cannot possibly fit` is `analyzed`, so it is exactly that shape.
    partial = re.search(r"(\d+) of them part-placed and still short", body)
    assert partial is not None, "an obligation with sittings it cannot finish went unsaid"
    assert 1 <= int(partial.group(1)) <= int(wont), (
        "part-placed work is a subset of what does not finish"
    )


def test_a_board_that_does_fit_keeps_the_plain_sentence(conn, client, settings) -> None:  # type: ignore[no-untyped-def]
    """The reconciliation is not a new permanent hedge. With nothing unreachable the
    original claim is true, and it is what renders."""
    from tests.test_planner import add_commitment

    add_commitment(conn, settings, "fits fine", minutes=30, due=DAY + timedelta(days=5))
    conn.commit()

    body = client.get(f"/schedule/runway?date={DAY.isoformat()}").text

    assert "every one of them with a day" in body
    assert "do not finish before they are due" not in body


def test_the_advice_is_stated_once_not_per_row(conn, client, settings) -> None:  # type: ignore[no-untyped-def]
    """Finding B's second half. It was rendered on every unreachable row and identical on
    all forty-four — 6.2KB of a 92KB page spent restating one sentence between the facts
    that actually differ."""
    from tests.test_planner import add_commitment

    for n in range(1, 4):
        add_commitment(conn, settings, f"cannot fit {n}", minutes=6_000,
                       due=DAY + timedelta(days=2), n=n, estimate_source="analyzed")
    conn.commit()

    body = client.get(f"/schedule/runway?date={DAY.isoformat()}").text

    assert body.count("cannot fit") >= 3, "the rows are there to be counted against"
    assert body.count("move the date") == 1


def test_the_row_advice_names_the_horizon_that_was_walked(conn, client, settings) -> None:  # type: ignore[no-untyped-def]
    """Finding B. `horizon_days` was `DEFAULT_HORIZON_DAYS` — the fortnight constant —
    while the route walks `solvency`, which goes to the last deadline on the board. Every
    unreachable row said "the next 14 days have nowhere to put it" over a walk that
    covered seventy-eight, and it was wrong in the direction that understates it."""
    from backglass.plan import runway as runway_mod
    from tests.test_planner import add_commitment

    add_commitment(conn, settings, "cannot possibly fit", minutes=6_000,
                   due=DAY + timedelta(days=40), estimate_source="analyzed")
    conn.commit()

    body = client.get(f"/schedule/runway?date={DAY.isoformat()}").text

    walked = int(re.search(r"all (\d+) days to their deadline", body).group(1))
    assert walked > runway_mod.DEFAULT_HORIZON_DAYS, (
        "the sentence is reporting the constant, not the horizon the walk covered"
    )
    assert f"next {runway_mod.DEFAULT_HORIZON_DAYS} days" not in body


def test_a_walk_offers_no_buttons(conn, client, settings) -> None:  # type: ignore[no-untyped-def]
    """Finding D. A travel block has a real `plan_block` row, so `block_id is not None`
    made it actionable and the canvas drew Done / Roll / Pin on a fifteen-pixel sliver.
    A walk is not an obligation: it exists because two rooms are far apart, there is
    nothing to mark done, and rolling it moves a consequence of geometry onto a day whose
    geometry is different."""
    from backglass.web.routes import schedule as schedule_mod

    walk = schedule_mod.Entry(
        title="Walk to Tempe WILOHAL 112", kind="fixed", start_label="10:15am",
        end_label="10:30am", top=195, height=15, lane=0, block_id=1565, travel=True,
    )
    work = schedule_mod.Entry(
        title="Write the essay", kind="work", start_label="1:00pm", end_label="2:00pm",
        top=0, height=60, lane=0, block_id=1566,
    )
    assert not walk.actionable
    assert work.actionable
