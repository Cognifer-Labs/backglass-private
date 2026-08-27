"""Submission state, which the ICS feed cannot carry. `backglass/canvas_enrich.py`.

Measured on the owner's ledger, 2026-08-27: thirteen assignments Canvas had already
graded were still open commitments, holding 434 minutes of planner capacity, one of them
on that morning's plan. `connectors/canvas_ics.py` predicted exactly this in its own
docstring — "work already handed in keeps reading as an open obligation" — and until now
nothing could tell the ledger otherwise.

Three properties matter more than any single field:

  * the join is Canvas's own assignment id and never a title;
  * the import never creates an assignment — Canvas lists 321 and the dated feed carries
    206, so an unmatched export row is the normal case, not an error;
  * the same export applied twice writes nothing the second time (rule 3).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backglass import canvas_enrich, coursework
from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID
from tests.test_coursework import parsed


def export(**kwargs: Any) -> dict[str, Any]:
    """One row in the shape the browser snippet writes."""
    base: dict[str, Any] = {
        "canvas_id": 7833000,
        "name": "1-1-1 - Tech in the 21st Century (12:35)",
        "points_possible": 5,
        "submitted_at": None,
        "submission_workflow_state": "unsubmitted",
        "score": None,
    }
    base.update(kwargs)
    return base


def records(*rows: dict[str, Any]) -> list[canvas_enrich.Record]:
    return [
        canvas_enrich.Record(
            canvas_id=int(row["canvas_id"]),
            points_possible=row.get("points_possible"),
            submitted_at=row.get("submitted_at"),
            submission_state=row.get("submission_workflow_state"),
            score=row.get("score"),
            title=str(row.get("name") or ""),
        )
        for row in rows
    ]


def _assignment(conn: Any, settings: Settings, *, with_commitment: bool = True) -> int:
    """One assignment row with the source item and commitment behind it, exactly as a
    real sync leaves them: the commitment is reached through `source_item_id`."""
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at, "
        " author, title, body_text, raw_json, content_hash) "
        "VALUES (?, 'canvas:ics', 'assignment:7833000', ?, '2026-08-25', 'CIS236', 't', "
        " 'b', '{}', 'hash')",
        (USER_ID, now_iso()),
    )
    item_id = int(conn.execute("SELECT id FROM source_item").fetchone()["id"])
    if with_commitment:
        conn.execute(
            "INSERT INTO commitment (user_id, direction, what, due_at, estimated_minutes, "
            " estimate_source, confidence, status, source_item_id, created_at) "
            "VALUES (?, 'i_owe', 'Complete 1-1-1 Tech in the 21st Century', '2026-08-25', "
            " 18, 'analyzed', 0.9, 'open', ?, ?)",
            (USER_ID, item_id, now_iso()),
        )
    coursework.upsert(conn, settings, "canvas:ics", [parsed()])
    return item_id


# ── what the export writes ────────────────────────────────────────────────


def test_points_and_submission_state_land_on_the_assignment(
    conn: Any, settings: Settings
) -> None:
    _assignment(conn, settings)

    report = canvas_enrich.apply(
        conn,
        records(export(points_possible=5, submitted_at="2026-08-26T00:01:16Z",
                       submission_workflow_state="graded", score=5)),
    )

    assert (report.matched, report.updated, report.unmatched) == (1, 1, 0)
    row = conn.execute("SELECT * FROM assignment").fetchone()
    assert row["points_possible"] == 5
    assert row["submission_state"] == "graded"
    assert row["score"] == 5
    assert row["enriched_at"]


def test_an_export_row_with_no_assignment_is_counted_never_inserted(
    conn: Any, settings: Settings
) -> None:
    """Canvas lists 321 assignments and the dated feed carries 206. Creating rows here
    would make this a second connector reading a source the first one already owns."""
    _assignment(conn, settings)

    report = canvas_enrich.apply(conn, records(export(canvas_id=99999999)))

    assert (report.matched, report.unmatched) == (0, 1)
    assert conn.execute("SELECT COUNT(*) AS n FROM assignment").fetchone()["n"] == 1


def test_a_second_apply_of_the_same_export_writes_nothing(
    conn: Any, settings: Settings
) -> None:
    """Rule 3, and the reason `enriched_at` is stamped only on a row that moved: bumping
    it on every import would make every re-run a write for all two hundred rows."""
    _assignment(conn, settings)
    rows = records(export(submission_workflow_state="graded", score=5))

    first = canvas_enrich.apply(conn, rows)
    stamp = conn.execute("SELECT enriched_at FROM assignment").fetchone()["enriched_at"]
    second = canvas_enrich.apply(conn, rows)

    assert (first.updated, second.updated, second.unchanged) == (1, 0, 1)
    after = conn.execute("SELECT enriched_at FROM assignment").fetchone()["enriched_at"]
    assert after == stamp


def test_a_dry_run_writes_nothing_at_all(conn: Any, settings: Settings) -> None:
    _assignment(conn, settings)

    report = canvas_enrich.apply(
        conn,
        records(export(submission_workflow_state="graded", score=5)),
        close_submitted=True,
        dry_run=True,
    )

    assert report.closed  # it says what it would have done
    row = conn.execute("SELECT * FROM assignment").fetchone()
    assert row["submission_state"] is None
    assert conn.execute("SELECT status FROM commitment").fetchone()["status"] == "open"


def test_the_next_feed_sync_does_not_undo_the_enrichment(
    conn: Any, settings: Settings
) -> None:
    """The landmine this shape exists to avoid. `coursework.upsert` rewrites `description`
    and every effort column on every sync, and launchd runs it every thirty minutes — so
    an enrichment written into a column the upsert owns would evaporate within the hour,
    with no error and nothing to see."""
    _assignment(conn, settings)
    canvas_enrich.apply(
        conn, records(export(submission_workflow_state="graded", score=5, points_possible=5))
    )

    coursework.upsert(conn, settings, "canvas:ics", [parsed()])

    row = conn.execute("SELECT * FROM assignment").fetchone()
    assert row["submission_state"] == "graded"
    assert row["score"] == 5
    assert row["points_possible"] == 5


# ── what counts as finished ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "row",
    [
        export(submission_workflow_state="graded", score=5),
        export(submission_workflow_state="submitted", submitted_at="2026-08-26T00:00:00Z"),
        export(submission_workflow_state="pending_review"),
        # An autograded activity records a mark and no submission event.
        export(submission_workflow_state="graded", score=4, points_possible=4),
        # Handed in and marked zero is finished work that went badly, not work to do.
        export(submission_workflow_state="graded", score=0,
               submitted_at="2026-08-26T00:00:00Z"),
    ],
)
def test_canvas_saying_the_work_is_in(conn: Any, settings: Settings, row: dict) -> None:
    _assignment(conn, settings)

    report = canvas_enrich.apply(conn, records(row))

    assert report.finished_still_open


def test_a_zero_with_no_submission_is_a_miss_not_a_finish(
    conn: Any, settings: Settings
) -> None:
    """The false positive the live export supplied. "Module 1 Scientific Reasoning
    Homework 1" came back `graded, 0/6` with no `submitted_at` and a due date three days
    out — Canvas scoring an empty slot, not a hand-in. Closing it would tell the owner
    they had done something they had not, in the place they go to find out."""
    _assignment(conn, settings)

    report = canvas_enrich.apply(
        conn,
        records(export(submission_workflow_state="graded", score=0, points_possible=6)),
        close_submitted=True,
    )

    assert report.closed == {}
    assert report.finished_still_open == []
    assert conn.execute("SELECT status FROM commitment").fetchone()["status"] == "open"


def test_work_canvas_has_not_taken_in_is_left_alone(
    conn: Any, settings: Settings
) -> None:
    _assignment(conn, settings)

    report = canvas_enrich.apply(conn, records(export()), close_submitted=True)

    assert report.closed == {}
    assert report.finished_still_open == []
    assert conn.execute("SELECT status FROM commitment").fetchone()["status"] == "open"


# ── closing the commitment, which is opt-in ───────────────────────────────


def test_finished_work_is_reported_but_not_closed_by_default(
    conn: Any, settings: Settings
) -> None:
    """Recording what Canvas said is safe. Resolving a commitment on the strength of it
    is a change to the board the owner reads every morning, so it asks."""
    _assignment(conn, settings)

    report = canvas_enrich.apply(
        conn, records(export(submission_workflow_state="graded", score=5))
    )

    assert len(report.finished_still_open) == 1
    assert report.closed == {}
    assert conn.execute("SELECT status FROM commitment").fetchone()["status"] == "open"


def test_close_submitted_resolves_it_and_says_why(conn: Any, settings: Settings) -> None:
    """Rule 1: the note carries where the claim came from, so a wrong close is legible
    rather than mysterious."""
    _assignment(conn, settings)

    canvas_enrich.apply(
        conn,
        records(export(submission_workflow_state="graded", score=5, points_possible=5,
                       submitted_at="2026-08-26T00:01:16Z")),
        close_submitted=True,
    )

    row = conn.execute("SELECT status, resolution_note, resolved_at FROM commitment").fetchone()
    assert row["status"] == "done"
    assert row["resolved_at"]
    assert row["resolution_note"] == "canvas: graded, submitted 2026-08-26, scored 5/5"


def test_a_score_reads_as_a_mark_not_a_float(conn: Any, settings: Settings) -> None:
    """`1.833333333333333` and `5.0` both came back from the live export; neither is how
    a mark is written down."""
    _assignment(conn, settings)

    canvas_enrich.apply(
        conn,
        records(export(submission_workflow_state="graded", score=1.833333333333333,
                       points_possible=2)),
        close_submitted=True,
    )

    note = conn.execute("SELECT resolution_note FROM commitment").fetchone()["resolution_note"]
    assert "scored 1.83/2" in note


def test_closing_twice_is_not_an_error(conn: Any, settings: Settings) -> None:
    """`actions.resolve` refuses a row that is not open — correctly, it is what turns a
    stale page's write into a 422. An import that re-reads the same export must not fall
    over on it."""
    _assignment(conn, settings)
    rows = records(export(submission_workflow_state="graded", score=5))

    canvas_enrich.apply(conn, rows, close_submitted=True)
    second = canvas_enrich.apply(conn, rows, close_submitted=True)

    assert second.closed == {}
    assert conn.execute("SELECT status FROM commitment").fetchone()["status"] == "done"


def test_an_assignment_with_no_commitment_behind_it_is_harmless(
    conn: Any, settings: Settings
) -> None:
    _assignment(conn, settings, with_commitment=False)

    report = canvas_enrich.apply(
        conn,
        records(export(submission_workflow_state="graded", score=5)),
        close_submitted=True,
    )

    assert report.matched == 1
    assert report.closed == {}


# ── reading the file ──────────────────────────────────────────────────────


def test_the_export_is_read_as_the_browser_writes_it(tmp_path: Path) -> None:
    path = tmp_path / "canvas.json"
    path.write_text(json.dumps([export(submission_workflow_state="graded", score=5)]))

    rows = canvas_enrich.load(path)

    assert [r.external_id for r in rows] == ["assignment:7833000"]
    assert rows[0].finished


def test_an_error_marker_in_the_export_is_skipped(tmp_path: Path) -> None:
    """The snippet appends `{error: 403, course: …}` for a shell it cannot read. A hand-
    made document with a hole in it is not a reason to refuse the rest of it."""
    path = tmp_path / "canvas.json"
    path.write_text(json.dumps([{"error": 403, "course": "CHM113"}, export()]))

    assert len(canvas_enrich.load(path)) == 1


def test_a_document_that_is_not_a_list_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "canvas.json"
    path.write_text(json.dumps({"assignments": []}))

    with pytest.raises(ValueError, match="JSON array"):
        canvas_enrich.load(path)
