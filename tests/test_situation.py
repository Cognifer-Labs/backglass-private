"""The evolving state doc — a rendering over the ledger, versioned by its own hash.

The owner asked for "an evolving state doc about me". The three things that can go wrong
are what this file is about:

- it becomes a **second store** of the facts, and drifts from the first (the render
  functions read `fact`, `commitment`, `claim_dependency` and nothing else);
- it writes a version **every time it runs**, and the history stops being a history
  (rule 3, and the reason the body carries no date);
- it says a fact is gone, or still current, **when it is not** — so both halves of
  `_changed` get a test, and so does the dependency section that is currently empty on
  every real ledger.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from backglass import claim_events, facts, situation
from backglass.config import Settings
from backglass.ledger import USER_ID
from backglass.web.app import create_app
from tests.conftest import panel_slice

DAY = date(2026, 8, 24)


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


def a_fact(
    conn: sqlite3.Connection,
    subject: str,
    key: str,
    value: str,
    *,
    created_at: str = "2026-08-01T00:00:00Z",
    status: str = "active",
) -> int:
    conn.execute(
        "INSERT INTO fact (user_id, subject, key, value, source, status, created_at)"
        " VALUES (?, ?, ?, ?, 'manual', ?, ?)",
        (USER_ID, subject, key, value, status, created_at),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def an_obligation(
    conn: sqlite3.Connection, what: str, *, due: str | None = None
) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, content_hash, triage_verdict)"
        " VALUES (?, 'apple-mail', ?, '2026-08-01T00:00:00Z', '2026-08-01T09:00:00-07:00',"
        " 'dean@asu.edu', 'A note', 'the body', ?, 'keep')",
        (USER_ID, f"m-{what}", f"h-{what}"),
    )
    item = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, due_at, confidence, status,"
        " estimated_minutes, estimate_source, source_item_id, created_at)"
        " VALUES (?, 'i_owe', ?, ?, 0.9, 'open', 30, 'manual', ?, '2026-08-01T00:00:00Z')",
        (USER_ID, what, due, item),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


class TestRendering:
    def test_an_empty_ledger_renders_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """A fresh install produces "", not a document of empty headers — the same
        property `context.assemble` holds, and for the same reason: a section with
        nothing in it is noise a reader has to skip past."""
        assert situation.render(conn, settings, DAY) == ""

    def test_every_fact_is_addressable_and_grouped_by_its_lane(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """`[fact N]` is the whole reason this is not `facts.owner_context`: a
        dependency or a drop has to cite a claim that has provenance."""
        fid = a_fact(conn, "identity", "enrolment", "ASU Tempe, Barrett")
        a_fact(conn, "housing", "dorm", "Willow Hall 502")
        body = situation.render(conn, settings, DAY)
        assert f"[fact {fid}] enrolment: ASU Tempe, Barrett" in body
        assert "  identity\n" in body and "  housing\n" in body

    def test_a_superseded_fact_shows_the_move_not_just_the_new_value(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The section the goal turns on. A fact that changed is the event that can
        retire an obligation, so the document has to show both sides of it."""
        old = a_fact(conn, "identity", "enrolment", "UT Dallas")
        new = facts.remember(conn, settings, "identity", "enrolment", "ASU Tempe")
        body = situation.render(conn, settings, DAY)
        assert f'[fact {old}] "UT Dallas" → [fact {new}] "ASU Tempe"' in body

    def test_a_retracted_fact_says_so_and_names_no_replacement(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Retraction and supersession are different events. Collapsing them would hide
        which one happened — the owner pulling a claim back is not the same as the
        world overtaking it."""
        fid = a_fact(conn, "housing", "dorm", "Willow Hall 502")
        facts.forget(conn, fid)
        body = situation.render(conn, settings, DAY)
        assert f'[fact {fid}] "Willow Hall 502" was retracted' in body
        assert "with nothing recorded in its place" in body

    def test_a_change_older_than_the_window_is_left_out(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """"What changed lately" has to mean lately, or the section grows without
        bound and stops being the thing a reader checks first."""
        stale = a_fact(
            conn, "identity", "school", "Hamilton High",
            created_at="2026-01-01T00:00:00Z", status="superseded",
        )
        body = situation.render(conn, settings, DAY)
        assert f"[fact {stale}]" not in body.split("WHO THE OWNER IS")[0]
        assert "WHAT CHANGED" not in body

    def test_two_renders_of_one_ledger_are_byte_identical(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Determinism is what makes `save` idempotent. A body that reordered itself
        between calls would write a version every sync and the diff would be noise."""
        a_fact(conn, "identity", "enrolment", "ASU Tempe")
        an_obligation(conn, "submit the housing form", due="2026-08-26")
        assert situation.render(conn, settings, DAY) == situation.render(conn, settings, DAY)

    def test_the_body_carries_no_date_so_an_idle_midnight_writes_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The guard the design decided on rather than inherited. An "as of 2026-08-24"
        line in the body would change the hash at every midnight, and the version list
        would become a log of how often the job ran — exactly what `save` exists to
        prevent. The day a version was rendered is its `created_at`."""
        a_fact(conn, "identity", "enrolment", "UT Dallas")
        facts.remember(conn, settings, "identity", "enrolment", "ASU Tempe")
        today = situation.render(conn, settings, DAY)
        tomorrow = situation.render(conn, settings, date(2026, 8, 25))
        # The dates that *are* in the body belong to the events — the day a fact changed
        # is part of what changed. What must not be there is the render's own day, and
        # the discriminator is that the two renderings agree byte for byte.
        assert "as of" not in today.lower()
        assert today == tomorrow
        assert situation.save(conn, today) is not None
        assert situation.save(conn, tomorrow) is None

    def test_the_week_section_reads_the_day_it_is_given(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Not vacuous: an evening Phoenix row is `2026-09-01T19:00:00-07:00`, which
        `date()` walks into the 2nd. The document must call it overdue on the 2nd and
        not on the 1st — the `substr(due_at, 1, 10)` rule, asserted from the outside."""
        an_obligation(conn, "return the lab key", due="2026-09-01T19:00:00-07:00")
        on_the_day = situation.render(conn, settings, date(2026, 9, 1))
        the_next_day = situation.render(conn, settings, date(2026, 9, 2))
        assert "OVERDUE" not in on_the_day
        assert "(due 2026-09-01 OVERDUE)" in the_next_day

    def test_include_facts_false_drops_the_who_section_and_nothing_else(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The relevance prompt already carries the facts under the header its citations
        are validated against; a second copy would leave the model choosing a list."""
        a_fact(conn, "identity", "enrolment", "UT Dallas")
        facts.remember(conn, settings, "identity", "enrolment", "ASU Tempe")
        full = situation.render(conn, settings, DAY)
        trimmed = situation.render(conn, settings, DAY, include_facts=False)
        assert "WHO THE OWNER IS" in full and "WHO THE OWNER IS" not in trimmed
        assert "WHAT CHANGED IN THE LAST" in trimmed


class TestWhatTheBoardRestsOn:
    def test_the_section_is_absent_when_nothing_has_recorded_a_dependency(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """An empty `claim_dependency` means "unknown", never "nothing depends on
        anything" — `claim_events.py`'s own rule. Rendering a section that said
        "0 obligations rest on facts" would state the second."""
        a_fact(conn, "identity", "enrolment", "ASU Tempe")
        an_obligation(conn, "submit the housing form")
        assert "WHAT THE OPEN OBLIGATIONS REST ON" not in situation.render(conn, settings, DAY)

    def test_it_counts_by_fact_and_shouts_when_the_fact_is_gone(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The point of the section: a wrong dependency is visible *before* it deletes
        anything, and a dependency still pointing at a superseded fact is the shape that
        retires an obligation."""
        fid = a_fact(conn, "identity", "enrolment", "UT Dallas")
        first = an_obligation(conn, "accept the AES award")
        second = an_obligation(conn, "pay the enrolment deposit")
        for cid in (first, second):
            claim_events.depends_on_fact(
                conn, subject_table="commitment", subject_id=cid, fact_id=fid,
                quote="award", reason="the offer is from UT Dallas",
            )
        body = situation.render(conn, settings, DAY)
        assert f"2 open · [fact {fid}] identity/enrolment" in body
        assert "2 of 2 open commitments judged" in body

        facts.remember(conn, settings, "identity", "enrolment", "ASU Tempe")
        # Superseding the fact breaks the dependencies through `invalidate_fact`, so the
        # section reports what is left standing rather than what used to be.
        assert "WHAT THE OPEN OBLIGATIONS REST ON" not in situation.render(conn, settings, DAY)


class TestVersions:
    def test_a_second_save_of_the_same_body_writes_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Rule 3 for this table. Two syncs over an unchanged ledger produce one
        version, not two, or the history stops being a history."""
        a_fact(conn, "identity", "enrolment", "ASU Tempe")
        body = situation.render(conn, settings, DAY)
        assert situation.save(conn, body) is not None
        assert situation.save(conn, body) is None
        assert conn.execute("SELECT COUNT(*) AS n FROM situation_doc").fetchone()["n"] == 1

    def test_a_moved_ledger_stores_a_version_and_says_what_moved(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        a_fact(conn, "identity", "enrolment", "UT Dallas")
        first, _ = situation.refresh(conn, settings, DAY)
        assert first is not None
        facts.remember(conn, settings, "identity", "enrolment", "ASU Tempe")
        second, moved = situation.refresh(conn, settings, DAY)
        assert second is not None and second.version_id != first.version_id
        assert any(line.startswith("- ") and "UT Dallas" in line for line in moved)
        assert any(line.startswith("+ ") and "ASU Tempe" in line for line in moved)

    def test_refresh_over_an_unchanged_ledger_reports_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        a_fact(conn, "identity", "enrolment", "ASU Tempe")
        situation.refresh(conn, settings, DAY)
        assert situation.refresh(conn, settings, DAY) == (None, [])

    def test_the_first_version_is_all_additions(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """`changes(None, v)` is the whole document arriving — otherwise the newest
        entry in a one-version history would render as an empty diff and read as
        "nothing happened"."""
        a_fact(conn, "identity", "enrolment", "ASU Tempe")
        version, moved = situation.refresh(conn, settings, DAY)
        assert version is not None
        assert moved and all(line.startswith("+ ") for line in moved)


class TestTheMemoryPage:
    def test_the_page_carries_the_doc_and_its_history(
        self, client: TestClient, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        a_fact(conn, "identity", "enrolment", "UT Dallas")
        situation.refresh(conn, settings, DAY)
        facts.remember(conn, settings, "identity", "enrolment", "ASU Tempe")
        conn.commit()
        panel = panel_slice(client.get("/memory").text, "panel-situation")
        assert "WHO THE OWNER IS" in panel
        assert "ASU Tempe" in panel
        assert "how it has changed" in panel

    def test_opening_the_page_never_stores_a_version(
        self, client: TestClient, conn: sqlite3.Connection
    ) -> None:
        """Reading must not write. A version per page view would make the history a log
        of how often the owner looked at it."""
        a_fact(conn, "identity", "enrolment", "ASU Tempe")
        conn.commit()
        for _ in range(3):
            assert client.get("/memory").status_code == 200
        assert conn.execute("SELECT COUNT(*) AS n FROM situation_doc").fetchone()["n"] == 0


class TestGroundTruthCanSeeIt:
    """`backglass state` is what CLAUDE.md says to trust over inference, so a versioned
    artifact it cannot see is a blind spot by construction."""

    def test_state_counts_the_versions_and_says_whether_the_doc_is_current(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass import state as state_mod

        a_fact(conn, "identity", "enrolment", "UT Dallas")
        situation.refresh(conn, settings, DAY)
        kb = state_mod.collect(conn, settings).sections["knowledge_base"]
        assert kb["situation_versions"].value == 1
        assert kb["situation_current"].value is True

        # The ledger moves and no sync has written the new reading yet. This is the claim
        # worth having: a stored version is not evidence that it still describes anything.
        facts.remember(conn, settings, "identity", "enrolment", "ASU Tempe")
        kb = state_mod.collect(conn, settings).sections["knowledge_base"]
        assert kb["situation_current"].value is False

    def test_a_probe_that_cannot_run_says_unknown_rather_than_zero(
        self, conn: sqlite3.Connection, settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """"A confident answer assembled from a missing input is the failure it exists to
        prevent" — state.py's own rule, applied to the claim this change adds."""
        from backglass import state as state_mod

        def boom(*_: object, **__: object) -> str:
            raise RuntimeError("the renderer fell over")

        a_fact(conn, "identity", "enrolment", "ASU Tempe")
        monkeypatch.setattr(situation, "render", boom)
        kb = state_mod.collect(conn, settings).sections["knowledge_base"]
        assert kb["situation_current"].value is None
        assert "fell over" in (kb["situation_current"].unknown or "")


class TestTheRelevanceJudgeReadsIt:
    def test_the_situation_reaches_the_prompt(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """A5: the judge saw atomic facts and not the shape of the week. The block is
        context, never a source of citable ids — the prompt says so and `parse`
        enforces it."""
        from backglass.extract import prompts
        from backglass.extract import relevance as relevance_mod

        fid = a_fact(conn, "identity", "enrolment", "UT Dallas")
        facts.remember(conn, settings, "identity", "enrolment", "ASU Tempe")
        work = relevance_mod.Work(
            facts=relevance_mod.facts_for(conn),
            commitments=[],
            situation=situation.render(conn, settings, DAY, include_facts=False),
        )
        _, dynamic = relevance_mod.render(work, prompt=prompts.load("check-relevance"))
        assert f'[fact {fid}] "UT Dallas" → ' in dynamic
        assert "WHO THE OWNER IS" not in dynamic

    def test_a_pass_with_no_situation_renders_the_version_1_shape(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The block is best-effort: the judge worked without it at version 1, and a doc
        that cannot render must not cost the pass its verdicts."""
        from backglass.extract import prompts
        from backglass.extract import relevance as relevance_mod

        a_fact(conn, "identity", "enrolment", "ASU Tempe")
        work = relevance_mod.Work(facts=relevance_mod.facts_for(conn), commitments=[])
        _, dynamic = relevance_mod.render(work, prompt=prompts.load("check-relevance"))
        assert "(nothing else recorded)" in dynamic


class TestTheCommandLine:
    def test_situation_prints_and_refresh_stores(
        self, settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The surface the owner actually types. `--refresh` writes; a bare read does
        not, which is the same boundary the page holds."""
        from typer.testing import CliRunner

        from backglass import __main__ as cli

        a_fact(conn, "identity", "enrolment", "ASU Tempe")
        conn.commit()
        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        runner = CliRunner()

        read = runner.invoke(cli.app, ["situation"])
        assert read.exit_code == 0 and "WHO THE OWNER IS" in read.stdout
        assert conn.execute("SELECT COUNT(*) AS n FROM situation_doc").fetchone()["n"] == 0

        stored = runner.invoke(cli.app, ["situation", "--refresh"])
        assert stored.exit_code == 0 and "stored" in stored.stdout
        again = runner.invoke(cli.app, ["situation", "--refresh"])
        assert "unchanged" in again.stdout
        assert conn.execute("SELECT COUNT(*) AS n FROM situation_doc").fetchone()["n"] == 1

        history = runner.invoke(cli.app, ["situation", "--versions"])
        assert history.exit_code == 0 and "v1" in history.stdout

    def test_versions_on_a_ledger_that_has_never_stored_one(
        self, settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The empty case, because pairing each version with the one before it is where
        an empty and a one-element history both go wrong — and this is the state every
        installation is in until its first sync after the migration."""
        from typer.testing import CliRunner

        from backglass import __main__ as cli

        a_fact(conn, "identity", "enrolment", "ASU Tempe")
        conn.commit()
        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        result = CliRunner().invoke(cli.app, ["situation", "--versions"])
        assert result.exit_code == 0 and "no versions stored yet" in result.stdout
