"""Reading the assignment instead of guessing at its title. Goal 4, increment A.

The failure these hold the line on is a measured one: on 2026-08-20, 120 of the owner's
143 open Canvas assignments carried the same `type_default:30`, so "Take PSY101 Exam 4
(Ch. 13-15) via LockDown Browser" and "Complete LearningCurve 14a" were the same half
hour to the planner. Every case below is a real row out of that feed.

Two properties matter more than any individual number:

  * a stated number always beats the type table, and the row keeps the words it read;
  * a second pass over an unchanged feed writes nothing at all (rule 3), including
    timestamps — which is why the column is `last_changed_at`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from backglass import claim_events, coursework
from backglass.config import Settings
from backglass.db import now_iso
from backglass.ledger import USER_ID


@dataclass(frozen=True)
class FakeParsed:
    """The shape `canvas_ics.ParsedAssignment` presents. `upsert` types it loosely on
    purpose — the ledger does not care which connector did the reading."""

    external_id: str
    course: str
    title: str
    due_at: str
    url: str = ""
    description: str = ""


def parsed(**kwargs: Any) -> FakeParsed:
    base: dict[str, Any] = {
        "external_id": "assignment:7833000",
        "course": "CIS236",
        "title": "1-1-1 - Tech in the 21st Century (12:35)",
        "due_at": "2026-08-25",
        "url": "https://canvas.asu.edu/calendar#assignment_7833000",
        "description": "Watch the video and answer any questions that appear.",
    }
    base.update(kwargs)
    return FakeParsed(**base)


# ── what the assignment states about itself ───────────────────────────────────


def test_a_video_runtime_in_the_title_is_the_estimate(settings: Settings) -> None:
    effort = coursework.effort_for("1-1-1 - Tech in the 21st Century (12:35)", "", settings)
    assert effort.minutes == 12 + 1 + coursework.VIDEO_OVERHEAD_MINUTES
    assert effort.basis == "stated_runtime"
    assert "12:35" in effort.quote


def test_a_time_of_day_is_not_a_runtime(settings: Settings) -> None:
    """`(2:30 PM)` says when, not how long. Without the guard every meeting-shaped title
    would report itself as two and a half hours of work."""
    effort = coursework.effort_for("Advising drop-in (2:30 PM)", "", settings)
    assert effort.basis.startswith("type:")


def test_a_chapter_range_beats_the_verb(settings: Settings) -> None:
    """The measured failure, exactly. `estimates.classify` reads "Take … Exam" as a form
    and answers 30 minutes; the chapters say three chapters of revision."""
    effort = coursework.effort_for(
        "Take PSY101 Exam 4 (Ch. 13-15) via LockDown Browser", "", settings
    )
    assert effort.minutes == 3 * coursework.MINUTES_PER_CHAPTER
    assert effort.basis == "stated_chapters"


def test_a_chapter_list_is_not_a_two_chapter_range(settings: Settings) -> None:
    """The owner's real Exam 4 is "Ch. 13, 14, and 15". The first version of this matched
    a range only, read that as 13 to 14, and quietly took a third off the revision it
    scheduled. A list and a range are the same fact written two ways."""
    effort = coursework.effort_for(
        "Exam 4 (Ch. 13, 14, and 15) - Requires Respondus LockDown Browser", "", settings
    )
    assert effort.minutes == 3 * coursework.MINUTES_PER_CHAPTER
    names = {
        m.name
        for m in coursework.materials_for("Exam 4 (Ch. 13, 14, and 15)", "")
    }
    assert "Ch. 13–15" in names


def test_the_largest_stated_size_wins_not_the_first(settings: Settings) -> None:
    """A CIS 236 milestone description runs 12,000 characters and states its size more
    than once — "Finalize your integrated team RFP (3 pages)" above "Maximum 10 pages for
    the report body". Which mention comes first in a wall of prose is not evidence."""
    effort = coursework.effort_for(
        "T - Final Analysis, RFP, and Presentation",
        "Finalize your integrated team RFP (3 pages). … Maximum 10 pages for the report "
        "body, which includes the 3-page RFP.",
        settings,
    )
    assert effort.basis == "stated_pages_written"
    assert effort.minutes == round(10 * coursework.WORDS_PER_PAGE / coursework.WORDS_PER_MINUTE)
    assert "10 pages" in effort.quote


def test_a_form_is_not_an_exam_because_its_instructions_say_exam(
    settings: Settings,
) -> None:
    """Both of these are real rows. "Excuse Note Submission Link & Instructions" came out
    of the first live pass as 120 minutes of revision and "Excused Absence Requests" as a
    90-minute lab, each read off a description rather than off what the thing is."""
    note = coursework.effort_for(
        "Excuse Note Submission Link & Instructions Fall 2026",
        "Submit this if you miss an exam or a lab.",
        settings,
    )
    assert note.basis == "type:form"
    assert note.minutes == coursework.defaults(settings)["form"]


def test_the_word_final_alone_does_not_make_an_exam(settings: Settings) -> None:
    """"T - Team Planning" became 120 minutes of revision on the first live pass, because
    its description called something "the final milestone". An exam says exam."""
    effort = coursework.effort_for(
        "T - Team Planning", "This is the final milestone before the draft.", settings
    )
    assert effort.basis != "type:exam"


def test_the_description_still_answers_when_the_title_says_nothing(
    settings: Settings,
) -> None:
    """Title-first is not title-only: half the feed's titles are bare identifiers."""
    effort = coursework.effort_for(
        "6-2-2", "Watch the recording and answer the questions.", settings
    )
    assert effort.basis == "type:video"


def test_a_word_count_takes_the_upper_bound(settings: Settings) -> None:
    """An assignment asking for 800 to 1000 words is finished at 1000."""
    effort = coursework.effort_for(
        "Submit reflection", "Write 800-1000 words on the reading.", settings
    )
    assert effort.minutes == round(1000 / coursework.WORDS_PER_MINUTE)
    assert effort.basis == "stated_words"


def test_reading_pages_and_written_pages_are_different_units(settings: Settings) -> None:
    reading = coursework.effort_for("Read for Thursday", "Read pp. 45-70.", settings)
    writing = coursework.effort_for("Memo", "Submit a 3 page memo.", settings)
    assert reading.basis == "stated_pages_read"
    assert reading.minutes == 26 * coursework.MINUTES_PER_PAGE_READ
    assert writing.basis == "stated_pages_written"
    assert writing.minutes > reading.minutes


def test_the_type_table_answers_only_when_nothing_is_stated(settings: Settings) -> None:
    effort = coursework.effort_for(
        "Complete LearningCurve 14a on psychological disorders", "", settings
    )
    assert effort.basis == "type:learningcurve"
    assert effort.minutes == coursework.defaults(settings)["learningcurve"]
    assert effort.quote == ""  # nothing was read, so nothing is quoted


def test_work_longer_than_a_sitting_is_counted_in_sessions(settings: Settings) -> None:
    """`planner.select` drops a candidate bigger than the day's remaining capacity, so an
    honest 250-minute milestone has to arrive as sittings or it never gets scheduled at
    all — the increment C clamp reads this number."""
    effort = coursework.effort_for(
        "Submit Final Analysis, RFP, and Presentation", "1500-2000 words.", settings
    )
    assert effort.sessions == 3
    assert effort.minutes > settings.max_block_minutes


# ── materials ─────────────────────────────────────────────────────────────────


def test_materials_name_the_tool_the_work_cannot_start_without() -> None:
    materials = coursework.materials_for(
        "Take PSY101 Exam 4 (Ch. 13-15) via LockDown Browser", ""
    )
    names = {m.name for m in materials}
    assert "Respondus LockDown Browser" in names
    assert "Ch. 13–15" in names


def test_every_material_carries_the_words_it_came_from() -> None:
    """Rule 1 inside this table. A material with no evidence is a guess the owner has to
    go and check by hand, which is the work this is supposed to remove."""
    materials = coursework.materials_for(
        "Watch the lecture",
        "See the [WeVideo User Guide] (http://links.asu.edu/WeVideo-Learner) for help.",
    )
    assert materials
    for material in materials:
        assert material.quote
    link = next(m for m in materials if m.kind == "link")
    assert link.detail == "http://links.asu.edu/WeVideo-Learner"
    assert link.name == "WeVideo User Guide"


def test_no_material_is_invented_from_an_empty_assignment() -> None:
    assert coursework.materials_for("Submit HW 4", "") == []


# ── the record ────────────────────────────────────────────────────────────────


def test_a_second_pass_over_an_unchanged_feed_writes_nothing(
    conn: Any, settings: Settings
) -> None:
    """Rule 3, asserted on the timestamps too: a column bumped on every fetch would make
    every sync a write for all 159 rows."""
    first = coursework.upsert(conn, settings, "canvas:ics", [parsed()])
    assert first.inserted == 1
    stamps = conn.execute("SELECT first_seen_at, last_changed_at FROM assignment").fetchone()

    second = coursework.upsert(conn, settings, "canvas:ics", [parsed()])
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 1)
    assert second.writes == 0
    after = conn.execute("SELECT first_seen_at, last_changed_at FROM assignment").fetchone()
    assert (after["first_seen_at"], after["last_changed_at"]) == (
        stamps["first_seen_at"],
        stamps["last_changed_at"],
    )


def test_a_due_date_that_moved_upstream_is_recorded_and_reported(
    conn: Any, settings: Settings
) -> None:
    """Five CIS 236 assignments had moved when this was built and nothing could say so:
    the re-read produced an immutable-item conflict, and a conflict propagates nothing."""
    coursework.upsert(conn, settings, "canvas:ics", [parsed(due_at="2026-08-23")])
    report = coursework.upsert(conn, settings, "canvas:ics", [parsed(due_at="2026-08-25")])

    assert report.updated == 1
    assert report.notes and "2026-08-23 → 2026-08-25" in report.notes[0]
    row = conn.execute("SELECT due_at FROM assignment").fetchone()
    assert row["due_at"] == "2026-08-25"


def test_an_edited_description_re_derives_the_effort_and_the_materials(
    conn: Any, settings: Settings
) -> None:
    coursework.upsert(conn, settings, "canvas:ics", [parsed(description="Watch it.")])
    coursework.upsert(
        conn,
        settings,
        "canvas:ics",
        [parsed(description="Now also read Ch. 4-6 and open Tableau.")],
    )
    names = {
        row["name"]
        for row in conn.execute("SELECT name FROM assignment_material")
    }
    assert "Tableau" in names
    row = conn.execute("SELECT description_hash, analyzed_hash FROM assignment").fetchone()
    assert row["description_hash"] == row["analyzed_hash"]


def test_a_material_the_text_no_longer_names_is_withdrawn(
    conn: Any, settings: Settings
) -> None:
    coursework.upsert(conn, settings, "canvas:ics", [parsed(description="Open Tableau.")])
    report = coursework.upsert(
        conn, settings, "canvas:ics", [parsed(description="Nothing needed.")]
    )
    assert report.materials_removed == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM assignment_material").fetchone()["n"] == 0


def test_the_model_layers_rows_are_not_this_layers_to_withdraw(
    conn: Any, settings: Settings
) -> None:
    """Increment D writes from evidence a regex cannot see, so "I did not derive it" is
    not a contradiction — the deterministic sweep must not delete it once a run."""
    coursework.upsert(conn, settings, "canvas:ics", [parsed(description="Nothing.")])
    assignment_id = conn.execute("SELECT id FROM assignment").fetchone()["id"]
    conn.execute(
        "INSERT INTO assignment_material (user_id, assignment_id, kind, name, detail, "
        " quote, basis, created_at) VALUES (?, ?, 'document', 'Dataset Evaluation', '', "
        " 'building on your previous work', 'model', ?)",
        (USER_ID, assignment_id, now_iso()),
    )
    report = coursework.upsert(conn, settings, "canvas:ics", [parsed(description="Nothing.")])
    assert report.materials_removed == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM assignment_material").fetchone()["n"] == 1


# ── into the commitment the planner reads ─────────────────────────────────────


def _assignment_with_commitment(
    conn: Any, settings: Settings, *, estimate_source: str | None, minutes: int | None
) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at, "
        " author, title, body_text, raw_json, content_hash) "
        "VALUES (?, 'canvas:ics', 'assignment:7833000', ?, '2026-08-25', 'CIS236', 't', "
        " 'b', '{}', 'hash')",
        (USER_ID, now_iso()),
    )
    item_id = conn.execute("SELECT id FROM source_item").fetchone()["id"]
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, estimated_minutes, "
        " estimate_source, confidence, status, source_item_id, created_at) "
        "VALUES (?, 'i_owe', 'Take PSY101 Exam 4', '2026-08-25', ?, ?, 0.9, 'open', ?, ?)",
        (USER_ID, minutes, estimate_source, item_id, now_iso()),
    )
    coursework.upsert(
        conn,
        settings,
        "canvas:ics",
        [parsed(title="Take PSY101 Exam 4 (Ch. 13-15) via LockDown Browser")],
    )
    return int(conn.execute("SELECT id FROM commitment").fetchone()["id"])


@pytest.mark.parametrize("source", [None, "type_default"])
def test_a_default_is_replaced_by_what_the_assignment_says(
    conn: Any, settings: Settings, source: str | None
) -> None:
    commitment_id = _assignment_with_commitment(
        conn, settings, estimate_source=source, minutes=30 if source else None
    )
    applied, notes = coursework.apply_estimates(conn)
    assert applied == 1 and notes
    row = conn.execute(
        "SELECT estimated_minutes, estimate_source FROM commitment WHERE id = ?",
        (commitment_id,),
    ).fetchone()
    assert row["estimated_minutes"] == 3 * coursework.MINUTES_PER_CHAPTER
    assert row["estimate_source"] == "analyzed"


@pytest.mark.parametrize("source", ["manual", "extracted"])
def test_a_number_somebody_chose_or_stated_outranks_this_one(
    conn: Any, settings: Settings, source: str
) -> None:
    """The ladder is manual > extracted > analyzed > type_default, and it ranks evidence
    rather than recency."""
    commitment_id = _assignment_with_commitment(
        conn, settings, estimate_source=source, minutes=25
    )
    applied, _ = coursework.apply_estimates(conn)
    assert applied == 0
    row = conn.execute(
        "SELECT estimated_minutes, estimate_source FROM commitment WHERE id = ?",
        (commitment_id,),
    ).fetchone()
    assert (row["estimated_minutes"], row["estimate_source"]) == (25, source)


def test_applying_the_same_estimate_twice_writes_nothing(
    conn: Any, settings: Settings
) -> None:
    _assignment_with_commitment(conn, settings, estimate_source=None, minutes=None)
    assert coursework.apply_estimates(conn)[0] == 1
    assert coursework.apply_estimates(conn)[0] == 0  # rule 3


def test_a_dry_run_reports_what_it_would_write_and_writes_none_of_it(
    conn: Any, settings: Settings
) -> None:
    report = coursework.upsert(
        conn,
        settings,
        "canvas:ics",
        [parsed(description="See the [WeVideo User Guide] (http://links.asu.edu/x).")],
        dry_run=True,
    )
    assert report.inserted == 1
    # Counted, not skipped: a dry run that under-reported its own writes would be worse
    # than no dry run at all.
    assert report.materials_added == 2  # the guide link, and WeVideo itself
    assert conn.execute("SELECT COUNT(*) AS n FROM assignment").fetchone()["n"] == 0


# ── the seam into the sync ────────────────────────────────────────────────────


class _FakeCanvas:
    """A connector that emits one item and carries the parsed assignment beside it.

    The whole point of the attribute seam: `sync._ingest` reads `connector.assignments`
    through `getattr` after the fetch, exactly as it reads `seen_chats` and
    `excluded_by_rule`, so a connector still emits SourceItems and nothing else.
    """

    name = "canvas:ics"
    cursor = None

    def __init__(self) -> None:
        self.assignments = [parsed()]

    def fetch(self, since: Any) -> Any:
        from backglass.connectors.base import SourceItem, content_hash

        yield SourceItem(
            source=self.name,
            external_id="assignment:7833000",
            occurred_at="2026-08-25",
            author="CIS236",
            title="CIS236 — 1-1-1",
            body_text="CIS236: 1-1-1 is due 2026-08-25.",
            raw_json="{}",
            content_hash=content_hash(
                author="CIS236", title="CIS236 — 1-1-1",
                body_text="CIS236: 1-1-1 is due 2026-08-25.", occurred_at="2026-08-25",
            ),
        )


def test_the_sync_records_the_assignment_beside_the_item(
    conn: Any, settings: Settings
) -> None:
    from backglass.sync import sync as run_sync
    from tests.conftest import FakeModel

    report = run_sync(conn, settings, [_FakeCanvas()], FakeModel(), extract=False)

    assert report.assignments_recorded == 1
    row = conn.execute("SELECT title, source_item_id FROM assignment").fetchone()
    assert row["title"] == "1-1-1 - Tech in the 21st Century (12:35)"
    # Linked, not floating: the estimate reaches the planner through the commitment, and
    # the commitment is found through the item.
    assert row["source_item_id"] is not None


def test_a_second_sync_over_the_same_feed_writes_nothing(
    conn: Any, settings: Settings
) -> None:
    from backglass.sync import sync as run_sync
    from tests.conftest import FakeModel

    run_sync(conn, settings, [_FakeCanvas()], FakeModel(), extract=False)
    second = run_sync(conn, settings, [_FakeCanvas()], FakeModel(), extract=False)

    assert second.assignment_writes == 0  # rule 3, through the sync rather than the unit


# ── the date, carried the last step ───────────────────────────────────────────
#
# The half of the Canvas loop that was missing until 2026-08-23. `upsert` wrote the moved
# due date into `assignment` and stopped there, so the ledger held both answers at once
# and the planner read the stale one.


def _moved(conn: Any, settings: Settings, *, feed_due: str, commitment_due: str) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at, "
        " author, title, body_text, raw_json, content_hash) "
        "VALUES (?, 'canvas:ics', 'assignment:7833000', ?, ?, 'CIS236', 't', 'b', '{}', 'h')",
        (USER_ID, now_iso(), commitment_due),
    )
    item_id = conn.execute("SELECT id FROM source_item").fetchone()["id"]
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, confidence, status, "
        " source_item_id, created_at) "
        "VALUES (?, 'i_owe', 'Complete CIS236 assignment 1-1-1', ?, 0.9, 'open', ?, ?)",
        (USER_ID, commitment_due, item_id, now_iso()),
    )
    coursework.upsert(conn, settings, "canvas:ics", [parsed(due_at=feed_due)])
    return int(conn.execute("SELECT id FROM commitment").fetchone()["id"])


def test_a_due_date_that_moved_upstream_reaches_the_commitment(
    conn: Any, settings: Settings
) -> None:
    """The live case. `assignment:7833000` moved 08-23 → 08-25 on 2026-08-21 and commitment
    356 still said 08-23 two days later, because an immutable-item conflict propagates
    nothing."""
    commitment_id = _moved(conn, settings, feed_due="2026-08-25", commitment_due="2026-08-23")
    applied, notes = coursework.apply_due_dates(conn)
    assert applied == 1 and notes
    row = conn.execute(
        "SELECT due_at FROM commitment WHERE id = ?", (commitment_id,)
    ).fetchone()
    assert str(row["due_at"])[:10] == "2026-08-25"


def test_the_move_is_recorded_in_the_change_ledger(conn: Any, settings: Settings) -> None:
    """Rule 1 applied to a date the owner was already shown. A deadline that changes with
    nothing anywhere saying so is the shape of failure this system is built against."""
    commitment_id = _moved(conn, settings, feed_due="2026-09-04", commitment_due="2026-08-31")
    coursework.apply_due_dates(conn)
    events = claim_events.since(conn, "commitment", commitment_id)
    assert [
        (e["field"], e["old_value"][:10], e["new_value"][:10], e["cause"]) for e in events
    ] == [("due_at", "2026-08-31", "2026-09-04", "canvas:due_moved")]


def test_a_second_pass_over_the_same_dates_writes_nothing(
    conn: Any, settings: Settings
) -> None:
    """Rule 3."""
    _moved(conn, settings, feed_due="2026-08-25", commitment_due="2026-08-23")
    assert coursework.apply_due_dates(conn)[0] == 1
    assert coursework.apply_due_dates(conn)[0] == 0


def test_a_time_of_day_on_the_commitment_is_not_a_move(
    conn: Any, settings: Settings
) -> None:
    """`commitment.due_at` may carry a time where the feed states a date. Comparing the
    strings would rewrite the row on every sync forever and report a move that never
    happened."""
    _moved(conn, settings, feed_due="2026-08-25", commitment_due="2026-08-25T13:00:00")
    assert coursework.apply_due_dates(conn)[0] == 0


def test_a_closed_commitment_is_history_and_is_not_rewritten(
    conn: Any, settings: Settings
) -> None:
    commitment_id = _moved(conn, settings, feed_due="2026-08-25", commitment_due="2026-08-23")
    conn.execute("UPDATE commitment SET status = 'done' WHERE id = ?", (commitment_id,))
    assert coursework.apply_due_dates(conn)[0] == 0
    row = conn.execute(
        "SELECT due_at FROM commitment WHERE id = ?", (commitment_id,)
    ).fetchone()
    assert str(row["due_at"])[:10] == "2026-08-23"


def test_a_dry_run_names_the_move_and_makes_none_of_it(
    conn: Any, settings: Settings
) -> None:
    commitment_id = _moved(conn, settings, feed_due="2026-08-25", commitment_due="2026-08-23")
    applied, notes = coursework.apply_due_dates(conn, dry_run=True)
    assert applied == 1 and notes
    row = conn.execute(
        "SELECT due_at FROM commitment WHERE id = ?", (commitment_id,)
    ).fetchone()
    assert str(row["due_at"])[:10] == "2026-08-23"
    assert claim_events.since(conn, "commitment", commitment_id) == []
