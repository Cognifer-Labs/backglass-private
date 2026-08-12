"""docs/06, and docs/11 §3, §4, §8.

Two things are being protected here that are easy to break and expensive to notice:

  - **Write-back actually writes.** docs/06: "If the dashboard is read-only it becomes
    decoration within a week." Each of the seven actions is asserted against the ledger,
    not against the response body.
  - **The review queue stays honest.** docs/11 §4: "Never make Accept the primary button,
    style it larger, or preselect it." That is a product requirement, so it is a test.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID, Ledger
from backglass.web import actions, panels
from backglass.web.app import create_app
from tests.conftest import panel_slice

TODAY = date.today()


def _strip_css_comments(css: str) -> str:
    """The sheet documents its own rules at length, and the rounding ruling's prose quotes
    the very declarations these tests scan for. Strip comments before matching or the
    explanation of a rule reads as a second instance of it."""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


@pytest.fixture
def client(conn, settings: Settings):  # type: ignore[no-untyped-def]
    """The app opens its own connections against the same file the fixture migrated."""
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


def commitment(conn, settings: Settings, **kwargs) -> int:  # type: ignore[no-untyped-def]
    ledger = Ledger(conn, settings)
    external = kwargs.get("external_id", f"m{kwargs.get('n', 1)}")
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at, "
        " author, title, body_text, raw_json, content_hash, triage_verdict) "
        "VALUES (?, 'gmail:personal', ?, ?, ?, 'Dana <dana@example.gov>', 'Plan', 'b', "
        " '{}', ?, 'keep')",
        (USER_ID, external, now_iso(), "2026-07-14T09:15:00-07:00", f"h-{external}"),
    )
    source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    entity_id = ledger.resolve_entity(kwargs.get("counterparty", "Dana <dana@example.gov>"))
    conn.execute(
        "INSERT INTO commitment (user_id, direction, counterparty_entity_id, what, due_at, "
        " confidence, status, source_item_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            USER_ID,
            kwargs.get("direction", "i_owe"),
            entity_id,
            kwargs.get("what", "revised migration plan"),
            kwargs.get("due_at", TODAY.isoformat()),
            kwargs.get("confidence", 0.9),
            kwargs.get("status", "open"),
            source_id,
            now_iso(),
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def status_of(conn, commitment_id: int) -> str:  # type: ignore[no-untyped-def]
    return str(
        conn.execute("SELECT status FROM commitment WHERE id = ?", (commitment_id,)).fetchone()[
            "status"
        ]
    )


# ── read ──────────────────────────────────────────────────────────────────


def test_the_page_renders_with_an_empty_ledger(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Backglass" in response.text


def test_every_panel_has_a_declarative_empty_state(client: TestClient) -> None:
    """docs/06 §Empty states. "Never 'You're all caught up! 🎉'"."""
    body = client.get("/").text
    for copy in (
        "Nothing open.",
        "Nothing outstanding.",
        "Nothing to review.",
        "No targets set. A goal without a target is inert.",
    ):
        assert copy in body, copy
    for banned in ("caught up", "🎉", "Great job", "Nice work"):
        assert banned not in body


def test_the_board_shows_commitments_with_provenance(
    client: TestClient, conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """docs/11 §Cross-cutting rule 1: provenance everywhere, no exceptions."""
    commitment(conn, settings, n=1, what="revised migration plan")
    body = client.get("/").text
    assert "revised migration plan" in body
    assert "https://mail.google.com/mail/u/0/#all/m1" in body


def test_low_confidence_is_in_the_review_queue_and_not_on_the_board(
    client: TestClient, conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """CLAUDE.md rule 2. A guess next to a fact is the confusion the queue exists to stop."""
    commitment(conn, settings, n=1, what="a guess", confidence=0.3)
    body = client.get("/").text
    review = panel_slice(body, "panel-review")
    board = panel_slice(body, "panel-board")
    assert "a guess" in review
    assert "a guess" not in board


class TestStaleFold:
    """Long-overdue rows fold into Stale instead of drowning the Overdue lane.

    The 120-day mail backfill delivered thirty months-old obligations that were
    answered before the ledger existed; they buried the three genuinely late rows.
    Presentation only: same cards, same actions, still counted open."""

    def test_the_boundary_is_the_configured_day_not_near_it(
        self, conn, settings: Settings
    ) -> None:  # type: ignore[no-untyped-def]
        """Both edges in one change (lessons.md): exactly stale_after_days late is
        still live; one more day folds."""
        from datetime import timedelta

        edge = (TODAY - timedelta(days=settings.stale_after_days)).isoformat()
        past = (TODAY - timedelta(days=settings.stale_after_days + 1)).isoformat()
        commitment(conn, settings, n=1, what="on the edge", due_at=edge)
        commitment(conn, settings, n=2, what="long gone", due_at=past)
        board = panels.board_panel(conn, settings, TODAY)
        assert [r["what"] for r in board.rows] == ["on the edge"]
        assert [r["what"] for r in board.meta["stale"]] == ["long gone"]

    def test_the_fold_renders_with_working_actions_and_an_honest_count(
        self, client: TestClient, conn, settings: Settings
    ) -> None:  # type: ignore[no-untyped-def]
        from datetime import timedelta

        past = (TODAY - timedelta(days=40)).isoformat()
        commitment(conn, settings, n=1, what="answered in April", due_at=past)
        commitment(conn, settings, n=2, what="late but live",
                   due_at=(TODAY - timedelta(days=2)).isoformat())
        board = panel_slice(client.get("/").text, "panel-board")
        assert "Stale (1)" in board
        assert "answered in April" in board
        assert "2 open" in board  # folded is still open; the count must not lie

        # Resolving from inside the fold is the whole point — drive the real door.
        stale_id = conn.execute(
            "SELECT id FROM commitment WHERE what = 'answered in April'"
        ).fetchone()["id"]
        done = client.post(f"/commitments/{stale_id}/resolve")
        assert done.status_code == 200
        assert "Stale (" not in done.text  # the fold vanishes with its last row
        status = conn.execute(
            "SELECT status FROM commitment WHERE id = ?", (stale_id,)
        ).fetchone()["status"]
        assert status == "done"

    def test_a_board_of_only_stale_rows_does_not_claim_nothing_open(
        self, client: TestClient, conn, settings: Settings
    ) -> None:  # type: ignore[no-untyped-def]
        from datetime import timedelta

        commitment(conn, settings, n=1, what="ancient",
                   due_at=(TODAY - timedelta(days=90)).isoformat())
        board = panel_slice(client.get("/").text, "panel-board")
        assert "Nothing open" not in board
        assert "Stale (1)" in board


def test_swimlanes_group_by_counterparty(conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    """docs/06: "Board grouped by status, swimlanes by counterparty."."""
    commitment(conn, settings, n=1, counterparty="Dana <dana@example.gov>")
    commitment(conn, settings, n=2, counterparty="Marcus <m@example.com>", what="deck")
    board = panels.board_panel(conn, settings, TODAY)
    lanes = panels.swimlanes(board.rows)
    assert set(lanes) == {"Dana", "Marcus"}


def test_the_sources_panel_names_a_failure(
    client: TestClient, conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """docs/11 §8: "Never let a failed source degrade quietly. The dangerous failure is
    not the error, it is a brief that looks complete and is not."."""
    from backglass.connectors import credentials

    credentials.mark_failed(conn, "gmail:personal", "invalid_grant")
    body = client.get("/").text
    assert "gmail:personal" in body
    assert "invalid_grant" in body
    assert "var(--vermilion)" in body, "the failing row keeps a vermilion keyline"
    # The failure banner became a sidebar alert (Phase 6 rework) — same loudness,
    # now on every page rather than only this one.
    assert "views are incomplete" in body


def test_a_low_kill_rate_is_called_out(client: TestClient, conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    """docs/06: "If it drops below 85 percent the rules have drifted and cost is about to
    climb."."""
    for index in range(10):
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at, "
            " content_hash, triage_verdict) VALUES (?, 'gmail:personal', ?, ?, ?, ?, ?)",
            (
                USER_ID,
                f"k{index}",
                now_iso(),
                "2026-07-14T00:00:00+00:00",
                f"kh{index}",
                "drop" if index < 5 else "keep",
            ),
        )
    assert "below 85%" in client.get("/").text


def test_relative_timestamps() -> None:
    from datetime import datetime, timedelta

    now = datetime.fromisoformat("2026-07-30T12:00:00+00:00")
    assert panels.relative(None) == "never"
    assert panels.relative((now - timedelta(seconds=30)).isoformat(), now) == "just now"
    assert panels.relative((now - timedelta(minutes=20)).isoformat(), now) == "20m ago"
    assert panels.relative((now - timedelta(hours=5)).isoformat(), now) == "5h ago"
    assert panels.relative((now - timedelta(days=3)).isoformat(), now) == "3d ago"


# ── write-back: the seven actions in docs/06 ──────────────────────────────


def test_resolve_writes_to_the_ledger(client: TestClient, conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    """docs/06: "Resolving a commitment on the board marks it done and tomorrow's brief
    reflects that."."""
    cid = commitment(conn, settings, n=1)
    response = client.post(f"/commitments/{cid}/resolve")
    assert response.status_code == 200
    assert status_of(conn, cid) == "done"
    assert "panel-board" in response.text, "the endpoint returns the re-rendered fragment"


def test_resolving_removes_it_from_tomorrows_brief(
    client: TestClient, conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """The docs/09 §Phase 3 exit criterion, in one test."""
    from backglass.brief import daily

    cid = commitment(conn, settings, n=1, what="revised migration plan")
    before = daily.build(conn, settings, TODAY)
    assert any("revised migration plan" in line.text for line in before.all_lines())

    client.post(f"/commitments/{cid}/resolve")

    after = daily.build(conn, settings, TODAY)
    assert not any("revised migration plan" in line.text for line in after.all_lines())


def test_acting_on_a_closed_commitment_is_refused_not_silent(
    client: TestClient, conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """A stale page's second click must 422 (which the failed-write strip shows), not
    report success over a zero-row UPDATE. Rule: never let a failure be quiet."""
    cid = commitment(conn, settings, n=1)
    assert client.post(f"/commitments/{cid}/resolve").status_code == 200
    for verb in (f"/commitments/{cid}/resolve", f"/commitments/{cid}/drop",
                 f"/commitments/{cid}/snooze/1", f"/review/{cid}/accept"):
        response = client.post(verb)
        assert response.status_code == 422, verb
        assert "already done" in response.json()["detail"]
    assert status_of(conn, cid) == "done", "the refusals wrote nothing"


def test_drop_tombstones_rather_than_deleting(
    client: TestClient, conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    cid = commitment(conn, settings, n=1)
    client.post(f"/commitments/{cid}/drop")
    assert status_of(conn, cid) == "dropped"
    assert conn.execute("SELECT COUNT(*) AS n FROM commitment").fetchone()["n"] == 1


def test_snooze_moves_the_date_and_counts_as_a_rollover(
    client: TestClient, conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """Snoozing something four times without noticing is what the docs/05 §7 drop-or-do
    question exists to interrupt."""
    cid = commitment(conn, settings, n=1, due_at="2026-07-30")
    client.post(f"/commitments/{cid}/snooze/3")
    row = conn.execute("SELECT due_at, rollover_count, status FROM commitment").fetchone()
    assert row["due_at"] == "2026-08-02"
    assert row["rollover_count"] == 1
    assert row["status"] == "open", "a snooze is not a resolution"


def test_accept_promotes_a_review_item(client: TestClient, conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    cid = commitment(conn, settings, n=1, confidence=0.4)
    client.post(f"/review/{cid}/accept")
    row = conn.execute("SELECT confidence, status, resolution_note FROM commitment").fetchone()
    assert row["confidence"] == 1.0
    assert row["status"] == "open"
    assert "0.40" in str(row["resolution_note"]), "the model's score is kept for the eval loop"


def test_reject_records_the_reason_category(
    client: TestClient, conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """docs/11 §4: four buttons, no free text. "The distribution tells you which part of
    the extraction prompt to fix."."""
    cid = commitment(conn, settings, n=1, confidence=0.4)
    client.post(f"/review/{cid}/reject/wrong_date")
    row = conn.execute("SELECT status, resolution_note FROM commitment").fetchone()
    assert row["status"] == "dropped"
    assert row["resolution_note"] == "rejected:wrong_date"


def test_an_unknown_reject_reason_is_refused(
    client: TestClient, conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    cid = commitment(conn, settings, n=1, confidence=0.4)
    assert client.post(f"/review/{cid}/reject/because_i_said_so").status_code == 422
    assert status_of(conn, cid) == "open"


def test_a_rejected_commitment_is_not_resurrected_by_re_extraction(
    conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """docs/11 §4: "Reject tombstones it so re-extraction does not resurrect it."

    The dedup pass has to see dropped rows for this to hold. It is the reason
    open_commitments_for_dedup.sql matches on ('open', 'dropped') and not on 'open' alone.
    """
    from backglass.extract.commitments import apply
    from backglass.extract.schemas import CommitmentExtraction

    cid = commitment(conn, settings, n=1, what="revised migration plan", confidence=0.4)
    actions.reject(conn, cid, "not_a_commitment")

    ledger = Ledger(conn, settings)
    again = CommitmentExtraction.model_validate(
        {
            "commitments": [
                {
                    "direction": "i_owe",
                    "counterparty": "Dana <dana@example.gov>",
                    "what": "revised migration plan",
                    "due_at": "2026-07-31",
                    "confidence": 0.95,
                    "evidence": "I'll send the revised migration plan.",
                }
            ]
        }
    )
    report = apply(
        again,
        source_item_id=1,
        occurred_at="2026-07-14T09:15:00-07:00",
        ledger=ledger,
        settings=settings,
    )
    assert report.inserted == 0
    assert report.deduped == 1
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM commitment WHERE status = 'open'").fetchone()[
            "n"
        ]
        == 0
    )


def test_a_done_commitment_does_not_block_the_same_promise_being_made_again(
    conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """The other half of the dedup ruling. A recurring promise must not silently vanish
    because an identical one was kept last month."""
    from backglass.extract.commitments import apply
    from backglass.extract.schemas import CommitmentExtraction

    cid = commitment(conn, settings, n=1, what="weekly status report")
    actions.resolve(conn, cid)

    again = CommitmentExtraction.model_validate(
        {
            "commitments": [
                {
                    "direction": "i_owe",
                    "counterparty": "Dana <dana@example.gov>",
                    "what": "weekly status report",
                    "confidence": 0.95,
                    "evidence": "Same again this week.",
                }
            ]
        }
    )
    report = apply(
        again,
        source_item_id=1,
        occurred_at="2026-07-21T09:15:00-07:00",
        ledger=Ledger(conn, settings),
        settings=settings,
    )
    assert report.inserted == 1


def test_tick_and_untick_persist(client: TestClient, conn) -> None:  # type: ignore[no-untyped-def]
    """docs/06: "Ticking a checklist item persists."."""
    conn.execute(
        "INSERT INTO checklist_item (user_id, title) VALUES (?, 'Inbox to zero')", (USER_ID,)
    )
    item_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    client.post(f"/checklist/{item_id}/tick")
    assert conn.execute("SELECT COUNT(*) AS n FROM checklist_tick").fetchone()["n"] == 1
    client.post(f"/checklist/{item_id}/tick")
    assert conn.execute("SELECT COUNT(*) AS n FROM checklist_tick").fetchone()["n"] == 1, (
        "ticking twice is idempotent"
    )
    client.post(f"/checklist/{item_id}/untick")
    assert conn.execute("SELECT COUNT(*) AS n FROM checklist_tick").fetchone()["n"] == 0


def test_the_checklist_cap_explains_itself(conn) -> None:  # type: ignore[no-untyped-def]
    """docs/04 C1. The cap is in application code so the error can say why."""
    for index in range(actions.CHECKLIST_CAP):
        actions.add_checklist_item(conn, f"item {index}")
    with pytest.raises(actions.ActionError, match="non-negotiables"):
        actions.add_checklist_item(conn, "one too many")


def test_block_outcome_and_pinning(client: TestClient, conn) -> None:  # type: ignore[no-untyped-def]
    conn.execute(
        "INSERT INTO day_plan (user_id, local_date, tz, capacity_minutes, generated_at) "
        "VALUES (?, ?, 'America/Phoenix', 480, ?)",
        (USER_ID, TODAY.isoformat(), now_iso()),
    )
    plan_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO plan_block (day_plan_id, starts_at, ends_at, kind, title) "
        "VALUES (?, ?, ?, 'work', 'Draft the plan')",
        (plan_id, f"{TODAY}T09:00:00-07:00", f"{TODAY}T10:00:00-07:00"),
    )
    block_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    client.post(f"/blocks/{block_id}/outcome/rolled")
    row = conn.execute("SELECT outcome, rollover_count FROM plan_block").fetchone()
    assert row["outcome"] == "rolled"
    assert row["rollover_count"] == 1

    client.post(f"/blocks/{block_id}/pin/1")
    assert conn.execute("SELECT pinned FROM plan_block").fetchone()["pinned"] == 1


def test_editing_an_estimate_marks_it_manual(
    client: TestClient, conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """`estimate_source` exists so a later change to the type-default table cannot
    silently overwrite a number the owner chose."""
    cid = commitment(conn, settings, n=1)
    client.post(f"/commitments/{cid}/estimate/90")
    row = conn.execute("SELECT estimated_minutes, estimate_source FROM commitment").fetchone()
    assert row["estimated_minutes"] == 90
    assert row["estimate_source"] == "manual"


def test_adjusting_a_weekly_target(client: TestClient, conn) -> None:  # type: ignore[no-untyped-def]
    conn.execute(
        "INSERT INTO goal (user_id, title, horizon, definition_of_done, created_at) "
        "VALUES (?, 'Ship v1', 'quarterly', 'in production', ?)",
        (USER_ID, now_iso()),
    )
    goal_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO target (goal_id, kind, title, weekly_count, created_at) "
        "VALUES (?, 'cadence', 'Ship something', 1, ?)",
        (goal_id, now_iso()),
    )
    target_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    client.post(f"/targets/{target_id}/weekly/3")
    assert conn.execute("SELECT weekly_count FROM target").fetchone()["weekly_count"] == 3


# ── B7 ────────────────────────────────────────────────────────────────────


def _a_brief_row(conn, *, sent: bool) -> int:  # type: ignore[no-untyped-def]
    conn.execute(
        "INSERT INTO brief (user_id, generated_for_date, kind, content_md, items_json, "
        " word_count, sent_at) VALUES (?, ?, 'daily', 'md', '[]', 10, ?)",
        (USER_ID, TODAY.isoformat(), "2026-07-30T06:00:12Z" if sent else None),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def test_the_tracking_pixel_records_the_first_open_only(client: TestClient, conn) -> None:  # type: ignore[no-untyped-def]
    """docs/05 B7: "A brief nobody opens is the signal that matters most."."""
    brief_id = _a_brief_row(conn, sent=True)

    response = client.get(f"/b/{brief_id}.gif")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/gif"
    assert "no-store" in response.headers["cache-control"]

    first = conn.execute("SELECT opened_at FROM brief").fetchone()["opened_at"]
    assert first
    client.get(f"/b/{brief_id}.gif")
    assert conn.execute("SELECT opened_at FROM brief").fetchone()["opened_at"] == first


def test_only_a_sent_brief_can_be_marked_opened(client: TestClient, conn) -> None:  # type: ignore[no-untyped-def]
    """The pixel is an unauthenticated GET by design — a mail client fetches it — so
    anyone who can reach the dashboard can call it for any id. A brief that was never
    delivered cannot have been opened, and "nobody opened it" is the one reading this
    metric exists to give."""
    brief_id = _a_brief_row(conn, sent=False)

    assert client.get(f"/b/{brief_id}.gif").status_code == 200  # still a pixel, never a 404
    assert conn.execute("SELECT opened_at FROM brief").fetchone()["opened_at"] is None

    conn.execute(
        "UPDATE brief SET sent_at = ? WHERE id = ?", ("2026-07-30T06:00:12Z", brief_id)
    )
    conn.commit()
    client.get(f"/b/{brief_id}.gif")
    assert conn.execute("SELECT opened_at FROM brief").fetchone()["opened_at"]


# ── the design system, and the dark patterns it rules out ─────────────────


def test_accept_and_reject_are_equal_weight(
    client: TestClient, conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """docs/11 §4: "Never make Accept the primary button, style it larger, or preselect
    it. The whole point of the queue is an honest judgment, and a nudge toward accepting
    turns it into a rubber stamp."."""
    commitment(conn, settings, n=1, confidence=0.4)
    body = client.get("/").text
    review = panel_slice(body, "panel-review")

    accepts = re.findall(r"<button[^>]*\bclass=\"btn accept\"[^>]*>", review)
    rejects = re.findall(r"<button[^>]*\bclass=\"btn reject\"[^>]*>", review)

    assert len(accepts) == 1
    # Rejecting is choosing a category, so the reject side is the four reason buttons.
    # A plain Reject used to sit here too, hardwired to `not_a_commitment` — it recorded
    # a reason the owner had not chosen, which is why it is gone.
    assert len(rejects) == len(actions.REJECT_REASONS) == 4
    for nudge in ("primary", "autofocus", "checked", "default"):
        assert nudge not in accepts[0], nudge
    # Accept must never outweigh the reject side; it may not be the only thing offered.
    assert review.index(">Accept<") < review.index("reject as")


def test_the_only_confirmation_is_on_drop(client: TestClient, conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    """docs/06 §Interaction rules: "No confirmation dialogs except for drop."

    docs/11 §3: "Never a confirmation dialog on resolve. The action is reversible and the
    dialog costs more over a year than the occasional misclick."
    """
    commitment(conn, settings, n=1)
    body = client.get("/").text
    for fragment in body.split("<button")[1:]:
        if "hx-confirm" in fragment:
            assert "/drop" in fragment, "only drop may confirm"


def test_the_drop_confirmation_never_reaches_a_native_dialog(client: TestClient) -> None:
    """The bug that made Drop look broken for a day.

    `hx-confirm` calls `window.confirm`, and the desktop shell's WKWebView implements no
    confirm panel — the call returns false, htmx cancels, and the request is never sent.
    The server logged zero drop requests across a day of clicking while resolve and
    snooze from the same card went through, because only Drop carries a dialog.

    So the assertion is not "Drop confirms" — the test above already holds that line —
    but that the confirmation is the page's own, reachable in any webview. A native
    dialog cannot be asserted from here; its absence can.
    """
    assert "/static/confirm.js" in client.get("/").text, "the page must load it"
    source = _strip_css_comments(client.get("/static/confirm.js").text)
    assert "htmx:confirm" in source, "the hook htmx offers for exactly this"
    assert "preventDefault" in source, "the native dialog must never be allowed to run"
    assert "issueRequest" in source, "and the answered request must still be sent"
    assert "confirm(" not in source, (
        "a call to window.confirm is the defect itself; in this webview it returns "
        "false and the write is silently dropped"
    )


def test_an_armed_drop_is_visible_without_spending_a_sixth_ink(client: TestClient) -> None:
    """A confirmation the owner cannot see is a button that ate a click. The armed state
    takes the full vermilion Drop's own hover already means, so §3's five inks stand."""
    css = _strip_css_comments(client.get("/static/dashboard.css").text)
    armed = [line for line in css.splitlines() if "danger[data-armed]" in line]
    assert armed, "an armed Drop looks the same as an unarmed one"
    assert "--vermilion" in armed[0], "destroy is vermilion's, and no other ink's"


def test_no_shadows_no_gradients_outside_the_hatch(client: TestClient) -> None:
    """design-system.md §8 rule 7. The 2026-08-06 rounding ruling amended this rule but
    re-opened only rounding: a drop shadow still reads as a different design language on
    sight, and the one gradient in the system is still the protected block's 45° hatch."""
    css = _strip_css_comments(client.get("/static/dashboard.css").text)
    assert "box-shadow" not in css
    assert "text-shadow" not in css
    for line in css.splitlines():
        if "gradient" in line:
            assert "repeating-linear-gradient" in line and "45deg" in line, line


def test_every_radius_comes_from_the_token_scale(client: TestClient) -> None:
    """The 2026-08-06 rounding ruling, §7. The scale is the point: a raw px radius is how
    a system with three steps becomes a system with nine, and it is invisible in review
    because each individual number looks reasonable. Every declaration therefore resolves
    through --radius-*, and the only literal permitted is the 0 that opts the ledger
    inputs out (exemption 4 — border:0 plus a bottom hairline is a line, not a box)."""
    stripped = _strip_css_comments(client.get("/static/dashboard.css").text)

    # Every spelling of the property, not just the shorthand: the per-corner longhands
    # (border-top-left-radius, and the logical border-start-start-radius family) round the
    # same corners and would slip past a scan anchored on "border-radius" alone.
    pattern = r"border-(?:[a-z]+-){0,2}radius\s*:\s*([^;}]+)"
    declarations = re.findall(pattern, stripped)
    assert declarations, "the sheet should declare radii"
    allowed = {"var(--radius-1)", "var(--radius-2)", "var(--radius-3)", "var(--radius-reel)"}
    for value in declarations:
        flat = " ".join(value.split())
        if flat == "0":
            continue
        # A value is one or more terms: each is either a scale token, a literal 0 for a
        # corner that stays square, or a calc() over a scale token. Checking that
        # "var(--radius" appears SOMEWHERE would pass "calc(var(--radius-1) + 7px)" and
        # every mixed value with a raw literal in it, which is the regression this guards.
        # calc() nests one level here — calc(var(--radius-reel) + 4px) — so the inner
        # paren has to be consumed or the term splits across the var()'s closing bracket.
        terms = re.findall(r"calc\((?:[^()]|\([^()]*\))*\)|\S+", flat)
        for term in terms:
            if term == "0" or term in allowed:
                continue
            calc = re.fullmatch(r"calc\(\s*(var\(--radius-[a-z0-9]+\))\s*[-+]\s*\d+px\s*\)", term)
            assert calc and calc.group(1) in allowed, (
                f"radius term outside the token scale: {term!r} in {flat!r}"
            )

    zeroed = [
        block
        for block in stripped.split("}")
        if re.search(r"border-(?:[a-z]+-){0,2}radius\s*:\s*0\s*(;|$)", block)
    ]
    for block in zeroed:
        assert "input" in block, f"only the ledger inputs opt out of rounding: {block!r}"


def test_the_boxed_vocabulary_is_rounded(client: TestClient) -> None:
    """"Implement the principle of rounding to all elements" is only checkable against a
    list, so this is the list: the components that ARE boxes. The exempt ones — the
    full-bleed banner, the panel seams, the pinned failure strip — are asserted square in
    test_the_full_bleed_surfaces_stay_square, and the two lists together are what "all"
    means here."""
    css = _strip_css_comments(client.get("/static/dashboard.css").text)

    def rule(selector: str) -> str:
        """Every block for this selector, joined. A component is declared more than once
        in this sheet (.wev is geometry near the week grid and a later size override), so
        matching only the first or last block asks the wrong question."""
        found = [
            block
            for block in css.split("}")
            if "{" in block and block.split("{")[0].replace("\n", " ").strip() == selector
        ]
        assert found, f"no rule for {selector!r}"
        return "".join(found)

    for selector in (
        ".chip",  # controls
        ".btn",
        "input,textarea,select",
        "button.toggle",
        ".salert",
        ".box",  # marks
        ".sq",
        ".legend i",
        ".hmf",
        ".track",
        ".wev",
        ".gcard",  # containers
        ".rev",
        ".tl",
        ".wk7",
        ".sws",
    ):
        assert "border-radius" in rule(selector), f"{selector} carries no radius"

    # Concentric nesting is structural, not a copied number: a container whose children
    # sit flush on its edge clips them rather than restating the arithmetic.
    for selector in (".gcard", ".wk7", ".sws", ".track"):
        assert "overflow:hidden" in rule(selector).replace(" ", ""), (
            f"{selector} rounds but does not clip its flush children"
        )


def test_every_content_unit_is_a_tile(client: TestClient) -> None:
    """§7b. "All content should be separated in some way, each having individual tiles."
    The list is the claim, so it is the test."""
    css = _strip_css_comments(client.get("/static/dashboard.css").text)
    tile = [b for b in css.split("}") if "--tile-line" in b and "background:var(--fill-mild)" in b]
    assert tile, "no tile rule"
    # Two blocks, not one, and the split is the point: ".tt li" and ".mlist li" are a class
    # plus an element, so keeping them in the grouped :is() lifted it to (0,1,1) and every
    # single-class rule written to refine it silently lost. They carry the same declarations
    # in their own block. Membership is the claim here; the specificity shape of the grouped
    # rule is asserted separately by test_the_tile_rule_never_outranks_a_state_wash.
    declared = "".join(tile)
    for selector in (
        ".row", ".card", ".src", ".chk", ".goal", ".tt li", ".rmrow", ".tot",
        # The four the first pass argued its way out of. The owner asked twice.
        ".sgoal", ".goalrow", ".stepx", ".gtgt", ".mlist li",
    ):
        assert selector in declared, f"{selector} is not a tile"
    for block in tile:
        flat = block.replace(" ", "")
        assert "border-radius:var(--radius-3)" in flat, "a tile block must set the radius"
        assert "border:var(--border)solid" in flat, "a tile keyline must carry the 2px weight"
    flat = tile[0].replace(" ", "")
    assert "border-radius:var(--radius-3)" in flat
    # 2026-08-07: "separated by MORE than a thin line". A 1px keyline is a thin line, so
    # the weight is the assertion — the ink may vary by category, the weight may not.
    assert "border:var(--border)solid" in flat, "a tile keyline must carry the 2px weight"


def test_the_tile_rule_never_outranks_a_state_wash(client: TestClient) -> None:
    """§7b: state beats kind beats category. That ordering is enforced by nothing but
    specificity, and :is() takes the specificity of its MOST SPECIFIC argument — so a single
    descendant selector in the tile list (".gcard .mlist li") lifts the whole rule to (0,2,1)
    and silently outranks `.src.cold` at (0,2,0). It did: a going-cold person lost their
    vermilion row and rendered as an ordinary tile, in both themes, with every test green.
    Every argument must therefore be a single class."""
    css = _strip_css_comments(client.get("/static/dashboard.css").text)
    tile = [b for b in css.split("}") if "--tile-line" in b and "background:var(--fill-mild)" in b]
    assert tile, "no tile rule"
    selector = tile[0].split("{")[0]
    inside = selector[selector.index(":is(") + 4 : selector.rindex(")")]
    for arg in (a.strip() for a in inside.split(",")):
        assert arg, "empty selector argument"
        # ONE class. Nothing else. The previous pattern was r"\.[\w-]+(\s+[a-z]+)?" — it
        # permitted a trailing bare element so that ".tt li" and ".mlist li" could stay in
        # the list, and that permission is the hole this test exists to close: a class plus
        # an element is (0,1,1), not (0,1,0), so the grouped rule outranked every
        # single-class rule written after it to refine it. Those two selectors now carry the
        # tile declarations in their own block instead.
        assert re.fullmatch(r"\.[\w-]+", arg), (
            f"tile selector argument {arg!r} is more specific than one class, which lifts "
            f"the whole :is() above the state washes"
        )


def test_a_tile_keyline_never_uses_a_wash_line_token(client: TestClient) -> None:
    """The one rule the tile register rests on, and the one an edit will quietly break.

    In dark mode every `--*-line` token resolves to `var(--rule)` — the wash contract is
    "full-ink fill, keyline in the rule colour", where the FILL carries the colour. A tile's
    fill is neutral and its keyline carries the colour, so a `--*-line` here collapses all
    four categories into one paper outline the moment the theme flips. It looks correct in
    light, which is exactly why it needs an assertion rather than an eye."""
    css = _strip_css_comments(client.get("/static/dashboard.css").text)
    for block in css.split("}"):
        if "--tile-line:" not in block:
            continue
        for value in re.findall(r"--tile-line\s*:\s*([^;}]+)", block):
            assert "-line)" not in value, (
                f"tile keyline uses a wash line token, which is var(--rule) in dark: "
                f"{value.strip()!r} in {block.split('{')[0].strip()!r}"
            )


def test_the_full_bleed_surfaces_stay_square(client: TestClient) -> None:
    """Exemptions 1-3 of the rounding ruling. A radius on a bar that runs to both edges of
    its panel leaves four paper nicks against the seam, and the failed-write strip is
    pinned to three edges of the viewport — rounding either is a regression in the
    opposite direction from the one this suite used to guard.

    **`.panel` and `.banner` left this list on 2026-08-07**, when the owner ruled the
    panel grid detached. The exemption was never about panels being large; it was about
    the seam — two rounded corners meeting across a shared edge leave a notch and
    nothing else. Panels no longer share edges: each carries its own keyline on four
    sides and stands apart, which is the layout a radius is for. The banner follows it,
    and rounds only the two corners it shares with the panel's top, because WebKit does
    not reliably clip a `<summary>` to its parent's radius. `.mast`, `.side` and `.sec`
    are still full-bleed against a page edge and still exempt.
    """
    css = _strip_css_comments(client.get("/static/dashboard.css").text)
    for selector in (".mast", ".side", ".sec"):
        # Every rule that TOUCHES the element, not only the one whose selector text equals
        # it. `details.panel > summary.banner` restyles the same bar, and an exact-match
        # check would wave a radius through there while claiming the bar stays square.
        found = [
            block
            for block in css.split("}")
            if "{" in block and re.search(rf"{re.escape(selector)}(?![\w-])", block.split("{")[0])
        ]
        assert found, f"no rule for {selector!r}"
        for block in found:
            assert "radius" not in block, f"{selector} should stay square: {block!r}"


def test_an_element_rounds_the_edges_it_is_not_pinned_to(client: TestClient) -> None:
    """The correction to exemption 3. A pinned element is not exempt — only its pinned
    edges are. Each of these meets an edge on some sides and is free on others, and a
    single-value radius on any of them would round a corner that has to stay flush."""
    css = _strip_css_comments(client.get("/static/dashboard.css").text)

    def radius_of(selector: str) -> str:
        """The LAST declaration wins, because that is what the cascade does. Reading the
        first match instead makes the assertion blind to exactly the edit it exists to
        catch — a later rule overriding one of these with a single-value radius."""
        winner: str | None = None
        for block in css.split("}"):
            if "{" in block and block.split("{")[0].replace("\n", " ").strip() == selector:
                found = re.search(r"border-radius\s*:\s*([^;}]+)", block)
                if found:
                    winner = " ".join(found.group(1).split())
        assert winner is not None, f"no radius on {selector!r}"
        return winner

    # bottom-pinned strip → top two corners; right-flush tabs → left two; column on an
    # axis → top two. Each is a four-value radius with a 0 on the pinned side.
    assert radius_of(".oops") == "var(--radius-3) var(--radius-3) 0 0"
    assert radius_of(".tl .now span") == "var(--radius-1) 0 0 var(--radius-1)"
    assert radius_of(".mpace span") == "var(--radius-1) 0 0 var(--radius-1)"
    assert radius_of(".mcol > i") == "var(--radius-1) var(--radius-1) 0 0"
    assert radius_of(".bar .b") == "0 var(--radius-1) var(--radius-1) 0"


def test_tokens_are_served_from_the_validated_source(client: TestClient) -> None:
    """docs/10 §Web layer: "CSS is design/tokens.css plus hand-written rules." Serving the
    real file means the palette the dashboard renders is the one validate-palette.mjs
    checks, not a copy that can drift from it."""
    tokens = client.get("/design/tokens.css")
    assert tokens.status_code == 200
    assert "--paper:        #FCF8EC" in tokens.text or "--paper:" in tokens.text
    body = client.get("/").text
    assert "/design/tokens.css" in body
    assert "#FCF8EC" not in body, "the page must not hard-code a token value"


def test_htmx_is_vendored_not_fetched_from_a_cdn(client: TestClient) -> None:
    """No third party in the request path for a page rendering the owner's commitments,
    and the dashboard works offline."""
    body = client.get("/").text
    assert "/static/htmx.min.js" in body
    assert "unpkg.com" not in body and "cdn." not in body
    assert client.get("/static/htmx.min.js").status_code == 200


def test_the_keyboard_map_matches_docs06(client: TestClient) -> None:
    """docs/06: "j/k to move between cards, x to resolve, r to open review queue."."""
    body = client.get("/").text
    for key in ("'j'", "'k'", "'x'", "'d'", "'s'", "'r'"):
        assert f"case {key}:" in body, key



def test_no_reject_control_hardwires_a_reason_the_owner_did_not_choose(
    client: TestClient, conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """The feedback loop's one hard requirement: a recorded reason is the owner's.

    A plain "Reject" button used to sit above the four categories, posting
    `/reject/not_a_commitment` — so a rejection made for any other reason was recorded as
    that one. `backglass audit corrections` maps the dominant reason to the file to edit,
    so invented answers do not merely dilute the distribution, they point at the wrong
    prompt. Every reject control must be labelled with the reason it sends.
    """
    commitment(conn, settings, n=1, confidence=0.4)
    review = panel_slice(client.get("/").text, "panel-review")

    posted = re.findall(r'hx-post="/review/\d+/reject/(\w+)"[^>]*>([^<]+)<', review)
    assert len(posted) == len(actions.REJECT_REASONS)
    for key, label in posted:
        assert key in actions.REJECT_REASONS, key
        # "Wrong date" names wrong_date; a button reading "Reject" names nothing.
        assert label.strip().lower() == actions.REJECT_REASONS[key], (key, label)


class TestPanelGrounds:
    """Each dashboard panel sits on its own light tint (owner ruling 2026-08-06).

    Two panels share a row and the banner separates them across; nothing separated
    them down, so once one ran past the other the page read as a single field with a
    rule through it.
    """

    CSS = Path(__file__).resolve().parents[1] / "backglass/web/static/dashboard.css"
    TOKENS = Path(__file__).resolve().parents[1] / "design/tokens.css"
    PANELS = ("today", "board", "awaiting", "review", "goals", "checklist")

    def _grounds(self) -> dict[str, str]:
        css = self.CSS.read_text()
        found = {}
        for panel in self.PANELS:
            match = re.search(rf"#panel-{panel}\{{background:var\((--[a-z0-9-]+)\)\}}", css)
            assert match, f"#panel-{panel} has no ground"
            found[panel] = match.group(1)
        return found

    def test_no_two_panels_share_a_ground(self) -> None:
        """The whole point is separation. Two panels on the same tint are two panels
        the tint cannot tell apart, and the pair most at risk is the one the ruling
        named: Today beside Commitments."""
        grounds = self._grounds()
        assert grounds["today"] != grounds["board"]
        assert len(set(grounds.values())) == len(self.PANELS)

    def test_every_ground_token_is_defined_in_both_themes(self) -> None:
        """The failure this pins was found in a browser, not a test: a background
        naming a token that does not exist is not an error anywhere — the declaration
        parses away and the panel silently paints paper. A token missing from only the
        dark blocks fails the same way for half the users of a two-theme system."""
        tokens = self.TOKENS.read_text()
        light, _, rest = tokens.partition("@media")
        for name in set(self._grounds().values()):
            assert f"{name}:" in light, f"{name} is not defined for the light theme"
            # Both dark surfaces: the media query and the explicit data-theme override.
            assert rest.count(f"{name}:") >= 2, f"{name} is missing from a dark theme block"

    def test_the_full_width_panel_stays_on_paper(self) -> None:
        """Sources runs below the grid with nothing beside it to be told apart from,
        so a tint there would be decoration rather than structure."""
        assert "#panel-sources{background" not in self.CSS.read_text()


class TestGeometryRules:
    """The floor under §7, which survived the rule being reversed.

    When corners were square this rule existed because WebKit rounds buttons and text
    fields by default, so squareness had to be re-declared on every control the UA
    styles. The reversal does not retire it — it inverts what it defends. The UA's
    radius is not the scale's radius, and a control that never mentions one now gets
    whatever WebKit picks rather than `--radius`. Either way the violation is an
    omission, which leaves nothing in the CSS for the other tests to assert against.
    """

    CSS = Path(__file__).resolve().parents[1] / "backglass/web/static/dashboard.css"

    def test_the_controls_the_browser_rounds_have_a_floor(self) -> None:
        css = self.CSS.read_text()
        assert re.search(
            r"button,\s*input,\s*textarea,\s*select\{border-radius:var\(--radius-2\)\}", css
        ), (
            "the UA-rounded controls need one rule putting them on the scale, "
            "regardless of which component classes reach them"
        )

    def test_the_scale_is_ordered(self) -> None:
        """Marks < controls < containers, and the reel keeps the mark step. A scale whose
        steps cross is three numbers, not a scale."""
        tokens = (Path(__file__).resolve().parents[1] / "design/tokens.css").read_text()
        values = {}
        for name in ("radius-1", "radius-2", "radius-3"):
            match = re.search(rf"--{name}:\s*(\d+)px\s*;", tokens)
            assert match, f"--{name} is not defined"
            values[name] = int(match.group(1))
        assert values["radius-1"] < values["radius-2"] < values["radius-3"]
        assert re.search(r"--radius-reel:\s*var\(--radius-1\)\s*;", tokens)


def test_the_stylesheet_closes_every_block_it_opens() -> None:
    """An unbalanced sheet swallows whatever is appended after it.

    `@media(max-width:420px)` sat unclosed at the end of this file for months and cost
    nothing, because it was last — a browser closes the sheet at EOF. It became a bug
    the moment a rule was appended below it: the whole corner scale landed inside a
    420px media query, applied nowhere anyone was looking, and nothing failed. The CSS
    served correctly, the selectors were right, and the page was simply square.

    Balance is what makes appending safe, so it is asserted rather than assumed.
    """
    import re as _re

    sheet = Path(__file__).resolve().parents[1] / "backglass/web/static/dashboard.css"
    css = sheet.read_text()
    stripped = _re.sub(r"/\*.*?\*/", "", css, flags=_re.S)
    depth = 0
    for line_no, line in enumerate(stripped.splitlines(), start=1):
        for char in line:
            depth += (char == "{") - (char == "}")
            assert depth >= 0, (
                f"dashboard.css closes a block it never opened, line {line_no}"
            )
    assert depth == 0, (
        f"dashboard.css leaves {depth} block(s) open; the next rule appended "
        f"below them will not apply"
    )


def test_every_bordered_box_is_on_the_radius_scale() -> None:
    """A keyline makes something a box, and a box takes a corner.

    The corner scale was applied component by component, from memory, and it missed
    exactly the boxes nobody thinks of while editing the component they belong to: the
    awaiting row carries its state on the ROW rather than on a card class, the protected
    block is a schedule row wearing a hatch, the timeline event is positioned rather than
    flowed. Each was reported by eye, one at a time, after shipping.

    So the sheet is asked instead: every rule that declares a full `border:` must have a
    radius somewhere, and a box that genuinely wants square corners has to say so here
    rather than by omission — which is indistinguishable from having been forgotten.
    """
    import re as _re

    sheet = Path(__file__).resolve().parents[1] / "backglass/web/static/dashboard.css"
    css = _re.sub(r"/\*.*?\*/", "", sheet.read_text(), flags=_re.S)

    #: Selectors whose squareness is deliberate. Empty on purpose — every entry needs a
    #: reason, and "it looked fine" is not one now that the system is curved.
    SQUARE_BY_DESIGN: set[str] = set()

    rounded, bordered = set(), {}
    for block in _re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        body = block.group(2)
        for selector in (s.strip() for s in block.group(1).split(",")):
            if not selector or selector.startswith("@"):
                continue
            if "border-radius" in body:
                rounded.add(selector)
            if _re.search(r"border:\s*\d+px\s+(solid|dashed)", body):
                bordered[selector] = block.group(0)[:70]

    def final(selector: str) -> str:
        """The compound that actually names the box, ignoring its ancestors."""
        return _re.split(r"[\s>+~]+", selector.strip())[-1]

    # A box inherits a corner from the rule that styles the same compound: `.src.cold`
    # is a `.src`, and `.rmst .track.wee` is a `.track.wee` wherever it happens to sit.
    covered = {final(r) for r in rounded}
    missing = {
        sel: rule
        for sel, rule in bordered.items()
        if sel not in rounded and sel not in SQUARE_BY_DESIGN
        and not any(final(sel).startswith(c) for c in covered)
    }
    assert not missing, (
        "these draw a keyline and no corner — put them on the scale in the corners "
        f"block, or name them in SQUARE_BY_DESIGN with a reason: {sorted(missing)}"
    )


class TestTheReviewQueueLeadsWithWhatADecisionChanges:
    """158 questions is a queue nobody finishes; 34 is one somebody might.

    The ordering was `confidence DESC` — a fact about how sure the extraction was, which
    is not a fact about the owner's day. Measured on the real store: of 158 rows awaiting
    review, 47 were `owed_to_me`, which `planner.candidates` never schedules, so answering
    one cannot change any plan ever; 77 more carried no date and would land in the
    overflow tail. Thirty-four bore on the coming week, scattered anywhere in the list.

    This orders the questions and touches nothing about the answers: docs/11 §4's
    equal-weight Accept and Reject are unchanged, because the rubber-stamp risk is in
    nudging which button gets pressed, not in which card is read first.
    """

    def _queue(self, conn, settings: Settings):  # type: ignore[no-untyped-def]
        return panels.review_panel(conn, settings)

    def test_what_bears_on_the_week_comes_before_what_does_not(
        self, conn, settings: Settings
    ) -> None:  # type: ignore[no-untyped-def]
        commitment(conn, settings, n=1, what="owed to me, very sure",
                   direction="owed_to_me", confidence=0.69, due_at=TODAY.isoformat())
        commitment(conn, settings, n=2, what="undated, quite sure",
                   confidence=0.65, due_at=None)
        commitment(conn, settings, n=3, what="due this week, least sure",
                   confidence=0.31, due_at=TODAY.isoformat())

        order = [r["what"] for r in self._queue(conn, settings).rows]

        # Least confident of the three, and first, because it is the only one whose
        # answer reaches a plan. Under the old ordering it was last.
        assert order[0] == "due this week, least sure"
        assert order.index("undated, quite sure") < order.index("owed to me, very sure")

    def test_work_owed_to_the_owner_sinks_below_undated_promises(
        self, conn, settings: Settings
    ) -> None:
        """`owed_to_me_is_never_scheduled` is a tested planner invariant, so these are
        the only rows in the queue whose answer provably cannot move a plan — not now
        and not later. An undated promise at least becomes schedulable if it is dated."""
        commitment(conn, settings, n=1, what="owed to me", direction="owed_to_me",
                   confidence=0.6, due_at=TODAY.isoformat())
        commitment(conn, settings, n=2, what="undated promise", confidence=0.6, due_at=None)

        order = [r["what"] for r in self._queue(conn, settings).rows]
        assert order == ["undated promise", "owed to me"]

    def test_the_header_counts_the_pressing_ones_not_the_pile(
        self, conn, settings: Settings
    ) -> None:
        commitment(conn, settings, n=1, what="due this week", confidence=0.5,
                   due_at=TODAY.isoformat())
        for i in range(2, 6):
            commitment(conn, settings, n=i, what=f"undated {i}", confidence=0.5, due_at=None)

        panel = self._queue(conn, settings)
        assert panel.meta["pressing"] == 1
        assert panel.meta["total"] == 5

    def test_the_divider_marks_where_the_answers_stop_mattering(
        self, client: TestClient, conn, settings: Settings
    ) -> None:  # type: ignore[no-untyped-def]
        commitment(conn, settings, n=1, what="due this week", confidence=0.5,
                   due_at=TODAY.isoformat())
        commitment(conn, settings, n=2, what="undated one", confidence=0.5, due_at=None)

        review = panel_slice(client.get("/").text, "panel-review")

        assert "a decision changes no plan this week" in review
        # Said, not hidden: every row is still rendered and still answerable.
        assert "due this week" in review and "undated one" in review
        assert review.index("due this week") < review.index("a decision changes no plan")
        assert review.index("a decision changes no plan") < review.index("undated one")

    def test_accept_and_reject_stay_equal_weight(
        self, client: TestClient, conn, settings: Settings
    ) -> None:  # type: ignore[no-untyped-def]
        """docs/11 §4, re-asserted here because this change touches the queue's shape:
        ordering the questions must not become a nudge toward one answer."""
        commitment(conn, settings, n=1, what="a guess", confidence=0.3)
        review = panel_slice(client.get("/").text, "panel-review")

        assert 'class="btn accept"' in review
        assert 'class="btn reject"' in review
        assert "primary" not in review
        assert "checked" not in review

    def test_a_plan_months_out_is_not_counted_as_pressing(
        self, conn, settings: Settings
    ) -> None:  # type: ignore[no-untyped-def]
        """Every low-confidence plan used to count toward the header because a plan is
        an hour on a day. So is an hour in October. Counting them all put 150 of 303 in
        the number — the same pile wearing a smaller label."""
        cid = commitment(conn, settings, n=1, what="due this week", confidence=0.5,
                         due_at=TODAY.isoformat())
        source_id = int(
            conn.execute(
                "SELECT source_item_id AS s FROM commitment WHERE id = ?", (cid,)
            ).fetchone()["s"]
        )
        for what, starts_at in (
            ("dinner this week", f"{TODAY.isoformat()}T19:00:00-07:00"),
            ("something in October", "2026-10-14T19:00:00-07:00"),
        ):
            conn.execute(
                "INSERT INTO engagement (user_id, kind, what, starts_at, ends_at,"
                " when_is_explicit, location, status, confidence, source_item_id, created_at)"
                " VALUES (?, 'social', ?, ?, NULL, 1, NULL, 'proposed', 0.5, ?, ?)",
                (USER_ID, what, starts_at, source_id, now_iso()),
            )

        panel = panels.review_panel(conn, settings)

        assert panel.meta["total"] == 3
        # The commitment due this week and the plan this week; not October.
        assert panel.meta["pressing"] == 2
