"""The assembled memory block, and the `compatible:` stamp machinery it rides in on.

Two halves. `context.assemble` is the three-tier picture (facts, people, situation)
every model call carries — it must be deterministic, ledger-only, capped, and empty on
a fresh install. `Prompt.stamps` + the pending queries' `:compatible_versions` are what
let a prompt improve without silently re-extracting a thousand-item ledger.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from backglass import context
from backglass.config import Settings
from backglass.db import query
from backglass.facts import remember
from backglass.ledger import USER_ID

TODAY = date(2026, 8, 18)


def _item(conn: sqlite3.Connection, external_id: str, occurred: str) -> int:
    cur = conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, content_hash, triage_verdict)"
        " VALUES (1, 'manual', ?, ?, ?, 'T', 'B', ?, 'keep')",
        (external_id, occurred, occurred, f"h-{external_id}"),
    )
    return int(cur.lastrowid)


def _person(conn: sqlite3.Connection, name: str, **cols: object) -> int:
    cur = conn.execute(
        "INSERT INTO entity (user_id, kind, canonical_name, role, org, tags_json)"
        " VALUES (1, 'person', ?, ?, ?, ?)",
        (name, cols.get("role"), cols.get("org"), json.dumps(cols.get("tags", []))),
    )
    return int(cur.lastrowid)


def _commitment(
    conn: sqlite3.Connection,
    what: str,
    *,
    due: str | None = None,
    entity_id: int | None = None,
    direction: str = "i_owe",
    occurred: str = "2026-08-10T00:00:00Z",
) -> int:
    sid = _item(conn, f"c-{what}", occurred)
    cur = conn.execute(
        "INSERT INTO commitment (user_id, direction, counterparty_entity_id, what,"
        " due_at, confidence, status, source_item_id, created_at)"
        " VALUES (1, ?, ?, ?, ?, 0.9, 'open', ?, ?)",
        (direction, entity_id, what, due, sid, occurred),
    )
    return int(cur.lastrowid)


class TestWhatHasAlreadyBeenSettled:
    """The tier added 2026-08-23, for the failure the other four cannot see.

    The ledger knew the owner had declined an application and no model call was told, so
    the next mail about it read as a fresh obligation.
    """

    def test_a_standing_decision_is_stated(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass import decisions

        decisions.record(
            conn,
            settings,
            title="BioBridge",
            choice="dropped it for the MLSBE summer program",
        )

        block = context.assemble(conn, settings, day=TODAY)

        assert "WHAT THE OWNER HAS ALREADY DECIDED" in block
        assert "BioBridge" in block
        assert "MLSBE" in block

    def test_an_identity_answer_does_not_crowd_out_a_real_decision(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Answering "are these two the same person" is a decision, and it is not this.

        It has already been applied — the merge happened — and there are enough of them to
        fill the section's whole budget. On the real ledger they took seven of eight slots.
        """
        from backglass import decisions

        for n in range(3):
            decisions.record(
                conn,
                settings,
                title=f"Are alias {n} and alias {n + 1} the same person?",
                choice="Same person",
                reasoning="answered in the questions surface · duplicate_entity",
            )
        decisions.record(conn, settings, title="BioBridge", choice="dropped it")

        block = context.assemble(conn, settings, day=TODAY)

        assert "BioBridge" in block
        assert "the same person" not in block

    def test_no_decisions_means_no_section(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        remember(conn, settings, "housing", "dorm", "Barrett", source="manual")

        assert "ALREADY DECIDED" not in context.assemble(conn, settings, day=TODAY)


class TestTheSemester:
    def test_the_course_codes_the_ledger_knows_are_named(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        for title in ("CHM 113 Lecture", "CHM 113 (Lab)", "BIO 181 Lecture"):
            conn.execute(
                "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
                " occurred_at, title, body_text, content_hash, triage_verdict)"
                " VALUES (1, 'calendar:asu', ?, ?, ?, ?, 'B', ?, 'keep')",
                (f"cal-{title}", "2026-08-10T00:00:00Z", "2026-08-10T00:00:00Z",
                 title, f"h-cal-{title}"),
            )

        block = context.assemble(conn, settings, day=TODAY)

        assert "THE OWNER'S CURRENT CLASSES" in block
        # One line per course, not per meeting, and sorted — the ordering `courses.load`
        # uses tracks the clock and would give a different block every run.
        assert block.count("- BIO 181") == 1
        assert block.index("- BIO 181") < block.index("- CHM 113")

    def test_an_org_shell_with_no_course_code_is_not_a_class(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, title, body_text, content_hash, triage_verdict)"
            " VALUES (1, 'calendar:asu', 'shell', ?, ?, 'TRN-ASUReady-UG', 'B', 'h', 'keep')",
            ("2026-08-10T00:00:00Z", "2026-08-10T00:00:00Z"),
        )

        assert "CURRENT CLASSES" not in context.assemble(conn, settings, day=TODAY)


class TestAFreshInstall:
    def test_an_empty_ledger_assembles_to_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Byte-for-byte the prompt sent before this existed: nobody inherits a
        behaviour change they have no data for."""
        assert context.assemble(conn, settings, day=TODAY) == ""


class TestTheSituation:
    def test_counts_overdue_and_due_soon(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _commitment(conn, "overdue thing", due="2026-08-01")
        _commitment(conn, "due soon thing", due="2026-08-20")
        _commitment(conn, "far future thing", due="2026-12-01")

        block = context.assemble(conn, settings, day=TODAY)
        assert "3 open commitments: 1 overdue, 1 due within 7 days" in block
        assert '"overdue thing" (due 2026-08-01 OVERDUE)' in block
        assert '"due soon thing" (due 2026-08-20)' in block
        # Beyond the horizon: counted, never spelled out.
        assert "far future thing" not in block

    def test_an_offset_datetime_due_at_is_judged_on_its_local_day(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """due_at mixes bare dates and offset datetimes; substr(1,10) is the day under
        both shapes. The broken window for a raw string compare is exactly the boundary
        day: "2026-08-25T10:00" sorts ABOVE the bare horizon "2026-08-25", so a
        datetime-shaped commitment due on the horizon day falls out of due-soon — and a
        due-today one out of today. Both fixtures sit on that boundary on purpose; a
        mid-window time passes against the broken query too and proves nothing
        (lessons, 2026-08-02)."""
        _commitment(conn, "due today late evening", due="2026-08-18T23:00:00+00:00")
        _commitment(conn, "due on the horizon day", due="2026-08-25T10:00:00+00:00")

        block = context.assemble(conn, settings, day=TODAY)
        assert "2 open commitments: 0 overdue, 2 due within 7 days" in block
        assert "OVERDUE" not in block
        assert '"due on the horizon day" (due 2026-08-25)' in block

    def test_direction_reads_as_who_owes_whom(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        pid = _person(conn, "Priya Sharma")
        _commitment(conn, "send the deck", due="2026-08-19", entity_id=pid)
        _commitment(
            conn, "review my essay", due="2026-08-19", entity_id=pid,
            direction="owed_to_me",
        )

        block = context.assemble(conn, settings, day=TODAY)
        assert 'the owner owes Priya Sharma: "send the deck"' in block
        assert 'they owe the owner Priya Sharma: "review my essay"' in block

    def test_upcoming_plans_and_waiting_questions_appear(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        sid = _item(conn, "e-dinner", "2026-08-15T00:00:00Z")
        conn.execute(
            "INSERT INTO engagement (user_id, kind, what, starts_at, status, confidence,"
            " source_item_id, created_at)"
            " VALUES (1, 'social', 'dinner with Sam', '2026-08-21T19:00:00', 'confirmed',"
            " 0.9, ?, '2026-08-15T00:00:00Z')",
            (sid,),
        )
        conn.execute(
            "INSERT INTO open_question (user_id, kind, subject_key, question, asked_at)"
            " VALUES (1, 'conflict', 'k1', 'Which class?', '2026-08-15T00:00:00Z')",
        )

        block = context.assemble(conn, settings, day=TODAY)
        assert '- plan: "dinner with Sam" on 2026-08-21' in block
        assert "1 question(s) waiting on the owner's answer" in block

    def test_a_resolved_question_is_not_waiting(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        conn.execute(
            "INSERT INTO open_question (user_id, kind, subject_key, question, status,"
            " asked_at) VALUES (1, 'conflict', 'k1', 'Q', 'answered', '2026-08-15T00:00:00Z')",
        )
        assert context.assemble(conn, settings, day=TODAY) == ""


class TestThePeople:
    def test_people_with_recent_evidence_appear_with_their_profile(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        pid = _person(conn, "Shawn Gathas", role="Counselor", org="BASIS", tags=["school"])
        _commitment(conn, "reply to Shawn", entity_id=pid, occurred="2026-08-10T00:00:00Z")

        block = context.assemble(conn, settings, day=TODAY)
        assert "PEOPLE AROUND THE OWNER" in block
        assert "- Shawn Gathas — Counselor, BASIS (school; last seen 2026-08-10)" in block

    def test_a_person_with_only_stale_evidence_is_out_of_the_picture(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        pid = _person(conn, "Old Contact")
        _commitment(conn, "ancient", entity_id=pid, occurred="2025-01-01T00:00:00Z")

        assert "Old Contact" not in context.assemble(conn, settings, day=TODAY)

    def test_most_evidence_first_and_the_cast_is_capped(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        busy = _person(conn, "Zara Busy")
        for i in range(3):
            _commitment(conn, f"thing {i}", entity_id=busy, occurred="2026-08-10T00:00:00Z")
        for i in range(context.PEOPLE_LIMIT):
            pid = _person(conn, f"Aaa Person{i:02d}")
            _commitment(conn, f"solo {i}", entity_id=pid, occurred="2026-08-10T00:00:00Z")

        block = context.assemble(conn, settings, day=TODAY)
        people = [
            line for line in block.splitlines()
            if line.startswith("- ") and ("Person" in line or "Zara" in line)
        ]
        # Despite sorting last alphabetically, the most-touched person leads.
        assert people[0].startswith("- Zara Busy")
        assert len(people) == context.PEOPLE_LIMIT

    def test_a_touchpoint_counts_as_evidence(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        pid = _person(conn, "Felipe Batalini")
        sid = _item(conn, "t-1", "2026-08-11T00:00:00Z")
        conn.execute(
            "INSERT INTO touchpoint (user_id, entity_id, kind, occurred_at, source_item_id,"
            " created_at) VALUES (1, ?, 'met', '2026-08-11', ?, '2026-08-11T00:00:00Z')",
            (pid, sid),
        )
        assert "Felipe Batalini" in context.assemble(conn, settings, day=TODAY)


class TestDeterminismAndBudget:
    def test_the_same_ledger_renders_the_same_block(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        remember(conn, settings, "identity", "college", "ASU Tempe")
        pid = _person(conn, "Priya Sharma", role="Advisor")
        _commitment(conn, "send the deck", due="2026-08-19", entity_id=pid)

        first = context.assemble(conn, settings, day=TODAY)
        assert first == context.assemble(conn, settings, day=TODAY)
        assert first.index("ABOUT THE OWNER") < first.index("PEOPLE AROUND THE OWNER")
        assert first.index("PEOPLE AROUND") < first.index("CURRENT SITUATION")

    def test_sections_respect_their_ceilings(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        for i in range(40):
            _commitment(conn, f"long obligation number {i} " + "x" * 60,
                        due=f"2026-08-{10 + (i % 9):02d}")
        block = context.assemble(conn, settings, day=TODAY)
        situation = block[block.index("CURRENT SITUATION"):]
        assert len(situation) <= context.NOW_CHARS + 80  # header + slack, like facts'

    def test_long_titles_are_clipped(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _commitment(conn, "a" * 200, due="2026-08-19")
        block = context.assemble(conn, settings, day=TODAY)
        assert "a" * 100 not in block
        assert "…" in block


class TestCompatibleStamps:
    """A prompt bump with `compatible:` must not re-open rows done under the old one."""

    def _prompt(self, tmp_path, *, compatible: str | None):  # type: ignore[no-untyped-def]
        from backglass.extract import prompts

        line = f"compatible: {compatible}\n" if compatible else ""
        (tmp_path / "p.md").write_text(
            f"---\nid: p\nversion: 2\n{line}---\n\n## Prompt\n\n```\nhello {{{{x}}}}\n```\n"
        )
        return prompts.load("p", tmp_path)

    def test_stamps_carry_the_compatible_versions(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        assert self._prompt(tmp_path, compatible="p@1").stamps == ("p@2", "p@1")
        assert self._prompt(tmp_path, compatible=None).stamps == ("p@2",)

    @pytest.mark.parametrize(
        ("stamped", "expected_pending"),
        [
            (None, True),          # never extracted → pending
            ("p@1", False),        # done under the compatible old version → stays done
            ("p@2", False),        # done under the current version → done
            ("p@19", True),        # a DIFFERENT stamp that merely contains "p@1" → pending
            ("other@9", True),     # some other prompt's stamp → pending
        ],
    )
    def test_pending_extraction_respects_the_compatible_list(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        tmp_path,  # type: ignore[no-untyped-def]
        stamped: str | None,
        expected_pending: bool,
    ) -> None:
        del settings
        prompt = self._prompt(tmp_path, compatible="p@1")
        sid = _item(conn, "x1", "2026-08-10T00:00:00Z")
        if stamped:
            conn.execute(
                "UPDATE source_item SET extraction_version = ? WHERE id = ?", (stamped, sid)
            )

        pending = conn.execute(
            query("pending_extraction"),
            {"user_id": USER_ID, "compatible_versions": ",".join(prompt.stamps)},
        ).fetchall()
        assert bool(pending) is expected_pending

    def test_a_stamp_inside_a_longer_stamp_does_not_count_as_done(
        self, conn: sqlite3.Connection, settings: Settings, tmp_path  # type: ignore[no-untyped-def]
    ) -> None:
        """The comma-wrapping's whole job: a row stamped `p@1` must stay pending when
        the accepted list is `p@19,p@2` — a bare substring probe finds "p@1" inside
        "p@19" and silently marks the row done under a version it never saw."""
        from backglass.extract import prompts

        del settings
        (tmp_path / "p.md").write_text(
            "---\nid: p\nversion: 19\ncompatible: p@2\n---\n\n## Prompt\n\n```\nhi {{x}}\n```\n"
        )
        prompt = prompts.load("p", tmp_path)
        sid = _item(conn, "x2", "2026-08-10T00:00:00Z")
        conn.execute(
            "UPDATE source_item SET extraction_version = 'p@1' WHERE id = ?", (sid,)
        )
        pending = conn.execute(
            query("pending_extraction"),
            {"user_id": USER_ID, "compatible_versions": ",".join(prompt.stamps)},
        ).fetchall()
        assert len(pending) == 1

    def test_the_live_extract_prompt_names_v9_compatible(self) -> None:
        """The v10 bump (context + facts) is additive; the 1,300 rows extracted at v9
        must not silently become pending again. Re-extraction is a deliberate decision:
        delete the `compatible:` line."""
        from backglass.extract import prompts
        from backglass.sync import EXTRACT_PROMPT

        p = prompts.load(EXTRACT_PROMPT)
        assert "extract-commitments@9" in p.compatible
        assert "{{owner_context}}" in p.text


class TestTheWiring:
    def test_render_parts_carries_the_background_block(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.extract import commitments as tier2
        from backglass.extract import prompts
        from backglass.sync import EXTRACT_PROMPT

        del conn
        prompt = prompts.load(EXTRACT_PROMPT)
        item = {
            "author": "a@example.com", "occurred_at": "2026-08-18", "title": "t",
            "body_text": "b", "raw_json": None,
        }
        _, rendered = tier2.render_parts(
            item, prompt=prompt, settings=settings, owner_context="OWNER FACTS HERE"
        )
        assert "BACKGROUND\nOWNER FACTS HERE" in rendered
        # And the block is never an empty labelled heading.
        _, bare = tier2.render_parts(item, prompt=prompt, settings=settings)
        assert "BACKGROUND\n(none)" in bare

    def test_the_background_rules_ride_in_the_cacheable_half(self) -> None:
        """Rules above placeholders are paid for once (v9's lesson); the block itself
        is dynamic. A rule that drifted below the split line would be re-billed on
        every call forever."""
        from backglass.extract import prompts
        from backglass.sync import EXTRACT_PROMPT

        static, dynamic = prompts.load(EXTRACT_PROMPT).split()
        assert "Never\nextract from it" in static or "Never extract from it" in static
        assert "{{owner_context}}" in dynamic
        assert "{{owner_context}}" not in static


class TestTheSituationNamesTheNearestEdgeOfNow:
    """Every model call in the pipeline reads this block as "the owner's current
    situation". Until 2026-08-24 it ordered by due date ascending, which sounds right and
    is exactly backwards: the overdue set only grows at its old end, so the five named
    rows were the five most-lapsed on the board, permanently. Measured on the live ledger
    that morning — 37 things due inside the week, and the block named five obligations
    from March and April and none of them (pipeline-audit-2026-08-21 §4a).
    """

    def test_what_is_due_soon_is_named_before_what_has_lapsed(
        self, conn: sqlite3.Connection
    ) -> None:
        _commitment(conn, "an ancient thing", due="2026-03-23")
        _commitment(conn, "due tomorrow", due="2026-08-26")

        block = context._situation(conn, date(2026, 8, 25))

        assert block.index("due tomorrow") < block.index("an ancient thing")

    def test_among_lapsed_rows_the_most_recent_wins(
        self, conn: sqlite3.Connection
    ) -> None:
        """A thing that lapsed yesterday is live. A thing that lapsed in March is either
        dead or already in the staleness queue being asked about, and either way it is not
        what the next message is about."""
        for index in range(context.LINE_LIMIT):
            _commitment(conn, f"ancient {index}", due=f"2026-03-{index + 10:02d}")
        _commitment(conn, "lapsed yesterday", due="2026-08-24")

        block = context._situation(conn, date(2026, 8, 25))

        assert "lapsed yesterday" in block
        assert "ancient 0" not in block

    def test_the_counts_still_cover_everything_the_lines_do_not(
        self, conn: sqlite3.Connection
    ) -> None:
        """The reordering must not hide anything: it changes which five are *named*, and
        the summary above them is still over the whole board."""
        for index in range(context.LINE_LIMIT + 3):
            _commitment(conn, f"ancient {index}", due=f"2026-03-{index + 10:02d}")

        block = context._situation(conn, date(2026, 8, 25))

        assert f"{context.LINE_LIMIT + 3} open commitments" in block
        assert f"{context.LINE_LIMIT + 3} overdue" in block

    def test_an_evening_row_belongs_to_the_day_it_was_written_in(
        self, conn: sqlite3.Connection
    ) -> None:
        """The `substr(due_at, 1, 10)` rule, asserted through the new ORDER BY: `date()`
        would walk an evening Phoenix row into the next day and file it under "due soon"
        when it is due today (lessons, 2026-08-01)."""
        _commitment(conn, "this evening", due="2026-08-25T19:00:00-07:00")

        block = context._situation(conn, date(2026, 8, 25))

        assert "(due 2026-08-25)" in block
        assert "OVERDUE" not in block
