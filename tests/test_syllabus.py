"""The reading a class expects you to have done before you turn up. `backglass/syllabus.py`.

Owner's ask, 2026-08-27: "reading for human event ... havent been schedul[ed]."

The Human Event is HON 171, a reading seminar with a text due before every meeting, and
none of it was in the ledger. The syllabus had been ingested and read — `extract-
commitments@10` produced four commitments out of it, two essays and two undated notes —
and the twenty-eight dated readings in its Course Schedule became nothing.

The fixture is the owner's real syllabus text, cut at the Course Schedule and carried
past the end of it on purpose: the last meeting used to run to the end of the document
and take the entire policies section with it as its title, so the fixture has to contain
the thing the parser must stop at.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from backglass import syllabus
from backglass.config import Settings
from tests.test_planner import PHOENIX

FIXTURE = Path(__file__).parent / "fixtures" / "hon171_course_schedule.txt"
TEXT = FIXTURE.read_text()


@pytest.fixture
def sett(settings: Settings) -> Settings:
    return settings.model_copy(update={"default_tz": PHOENIX})


def parsed():  # type: ignore[no-untyped-def]
    return syllabus.readings(TEXT, year=2026)


# ── what it finds ─────────────────────────────────────────────────────────


def test_it_finds_the_semester() -> None:
    found, _ = parsed()
    assert len(found) == 23


def test_the_readings_are_the_ones_the_syllabus_names() -> None:
    found, _ = parsed()
    by_date = {r.due: r.title for r in found}

    assert by_date[date(2026, 9, 1)] == "The Epic of Gilgamesh"
    assert by_date[date(2026, 10, 6)] == "Virgil, The Aeneid"
    assert by_date[date(2026, 10, 20)] == "Beowulf"
    assert by_date[date(2026, 11, 10)] == "Dante Alighieri, Inferno"


def test_a_text_read_over_two_meetings_is_two_obligations() -> None:
    """Gilgamesh is discussed on the 1st and again on the 3rd, and the reading for the
    second meeting is real work — a plan that scheduled it once would be a plan that
    thinks the seminar met once."""
    found, _ = parsed()
    gilgamesh = [r.due for r in found if r.title == "The Epic of Gilgamesh"]
    assert gilgamesh == [date(2026, 9, 1), date(2026, 9, 3)]


def test_three_myths_on_one_day_are_one_reading() -> None:
    """The 27th assigns three creation stories. They are one evening's reading, and one
    entry in the table — splitting on the quotation marks would put three obligations on
    a day the syllabus gave one."""
    found, _ = parsed()
    day = next(r for r in found if r.due == date(2026, 8, 27))
    assert "Haudenosaunee" in day.title
    assert "Fulani" in day.title
    assert "Yoruba" in day.title


def test_the_citation_is_not_part_of_the_title() -> None:
    found, _ = parsed()
    aeneid = next(r for r in found if r.due == date(2026, 10, 6))
    assert aeneid.title == "Virgil, The Aeneid"
    assert "Vol." in aeneid.quote, "the citation is kept as evidence, just not as a name"


def test_the_next_weeks_heading_does_not_join_the_title() -> None:
    """Without the WEEK cut, "Beowulf" is recorded as "Beowulf WEEK 11"."""
    found, _ = parsed()
    assert all("WEEK" not in r.title.upper() for r in found)


# ── what it refuses ───────────────────────────────────────────────────────


def test_workshops_and_breaks_are_not_reading() -> None:
    found, skipped = parsed()
    titles = [r.title for r in found]
    assert not any("Workshop" in t for t in titles)
    assert not any("Break" in t for t in titles)
    assert any("Fall Break" in s for s in skipped), "and it says which ones it dropped"


def test_the_essay_deadlines_are_left_to_the_extractor_that_already_found_them() -> None:
    """Commitments 504 and 505 exist. Creating them again here would be the same promise
    on the board twice."""
    found, _ = parsed()
    assert not any("Essay" in r.title for r in found)
    assert not any("Paper" in r.title for r in found)


def test_the_parser_stops_at_the_end_of_the_table() -> None:
    """The last meeting used to run to the end of the document — "Final Paper due Course
    Policies and Guidelines Technical support . This course uses Canvas…" — and put the
    syllabus's whole policies section on the board as the name of an obligation."""
    found, _ = parsed()
    assert all(len(r.title) < 160 for r in found)
    assert not any("Canvas to deliver content" in r.title for r in found)


def test_a_document_with_no_schedule_returns_nothing_rather_than_a_guess() -> None:
    found, skipped = syllabus.readings(
        "Grading policy. Attendance. Contact the instructor.", year=2026
    )
    assert found == []
    assert skipped == []


def test_meetings_already_past_are_not_put_on_the_board_as_overdue() -> None:
    """A reading for a class three weeks ago is history, not an obligation, and handing
    the planner a pile of it would be handing it work nobody can do anything about."""
    found, _ = syllabus.readings(TEXT, year=2026, on_or_after=date(2026, 10, 1))

    assert found
    assert min(r.due for r in found) >= date(2026, 10, 1)


# ── writing them down ─────────────────────────────────────────────────────


def test_promotion_writes_one_commitment_per_reading(conn, sett) -> None:  # type: ignore[no-untyped-def]
    from backglass.db import now_iso
    from backglass.ledger import USER_ID

    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, raw_json, content_hash, triage_verdict) "
        "VALUES (?, 'files', 'syl-1', ?, '2026-08-21T00:00:00-07:00', ?, ?, '{}', "
        " 'h1', 'keep')",
        (USER_ID, now_iso(), "HON 171 Syllabus - Fall 2026.pdf", TEXT),
    )
    conn.commit()

    written, _ = syllabus.promote_from_ledger(conn, sett, on_or_after=date(2026, 8, 24))
    conn.commit()

    # 23, not the 22 the live run wrote: this cutoff includes the 25th, and the live
    # run was made on the 27th. The number moves with the day, which is the point of
    # `on_or_after` — so it is asserted against the parse rather than against a memory.
    assert written == len(syllabus.readings(TEXT, year=2026, on_or_after=date(2026, 8, 24))[0])
    row = conn.execute(
        "SELECT what, due_at, estimated_minutes, estimate_source, confidence "
        "FROM commitment WHERE due_at = '2026-09-01'"
    ).fetchone()
    assert row["what"] == "Read for HON 171, Tue 1 Sep: The Epic of Gilgamesh"
    assert row["estimated_minutes"] == sett.reading_minutes
    assert row["estimate_source"] == "analyzed", "the planner must treat reading as homework"
    assert row["confidence"] == 1.0, "nothing here was inferred"


def test_a_second_run_writes_nothing(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """CLAUDE.md rule 3. There is no external id to key on — a syllabus is one document
    listing thirty obligations, not thirty documents — so identity is what the obligation
    *is*: the course, the date and the text."""
    from backglass.db import now_iso
    from backglass.ledger import USER_ID

    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, raw_json, content_hash, triage_verdict) "
        "VALUES (?, 'files', 'syl-1', ?, '2026-08-21T00:00:00-07:00', ?, ?, '{}', "
        " 'h1', 'keep')",
        (USER_ID, now_iso(), "HON 171 Syllabus - Fall 2026.pdf", TEXT),
    )
    conn.commit()
    syllabus.promote_from_ledger(conn, sett, on_or_after=date(2026, 8, 24))
    conn.commit()

    again, _ = syllabus.promote_from_ledger(conn, sett, on_or_after=date(2026, 8, 24))

    assert again == 0


def test_a_file_that_is_not_a_syllabus_is_left_alone(conn, sett) -> None:  # type: ignore[no-untyped-def]
    from backglass.db import now_iso
    from backglass.ledger import USER_ID

    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, raw_json, content_hash, triage_verdict) "
        "VALUES (?, 'files', 'other', ?, '2026-08-21T00:00:00-07:00', ?, ?, '{}', "
        " 'h2', 'keep')",
        (USER_ID, now_iso(), "Lab safety handout.pdf", TEXT),
    )
    conn.commit()

    written, _ = syllabus.promote_from_ledger(conn, sett, on_or_after=date(2026, 8, 24))

    assert written == 0


def test_two_meetings_on_one_text_are_two_distinguishable_obligations(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """A seminar routinely spends two meetings on one text — Dante on 10 and 12 November,
    Chaucer on 17 and 19 — and those are two evenings of reading with two deadlines.

    Named without the date they were two rows reading "Read for HON 171: Dante Alighieri,
    Inferno", and `backglass duplicates` scored that pair 1.00 and offered to drop one.
    A deduplicator cannot be blamed for that; the names were genuinely identical, and the
    fix belongs where the name is made.
    """
    from backglass.db import now_iso
    from backglass.ledger import USER_ID

    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, raw_json, content_hash, triage_verdict) "
        "VALUES (?, 'files', 'syl-1', ?, '2026-08-21T00:00:00-07:00', ?, ?, '{}', "
        " 'h1', 'keep')",
        (USER_ID, now_iso(), "HON 171 Syllabus - Fall 2026.pdf", TEXT),
    )
    conn.commit()
    syllabus.promote_from_ledger(conn, sett, on_or_after=date(2026, 8, 24))
    conn.commit()

    dante = [
        str(row["what"])
        for row in conn.execute(
            "SELECT what FROM commitment WHERE what LIKE '%Inferno%' ORDER BY due_at"
        )
    ]
    assert len(dante) == 2
    assert dante[0] != dante[1], "two evenings of Dante, one name between them"

