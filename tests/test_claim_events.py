from __future__ import annotations

import sqlite3

from backglass import claim_events, facts
from backglass.config import Settings
from backglass.ledger import USER_ID
from backglass.web import actions


def _open_commitment(conn: sqlite3.Connection, what: str = "test commitment") -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash, triage_verdict)"
        " VALUES (?, 'gmail:personal', ?, datetime('now'), '2026-07-14T09:15:00-07:00',"
        " 'Dana <dana@example.gov>', 'Plan', 'b', '{}', ?, 'keep')",
        (USER_ID, f"m-{what}", f"h-{what}"),
    )
    source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    cur = conn.execute(
        "INSERT INTO commitment (user_id, direction, what, confidence, status,"
        " source_item_id, created_at) VALUES (?, 'i_owe', ?, 1.0, 'open', ?, datetime('now'))",
        (USER_ID, what, source_id),
    )
    return int(cur.lastrowid or 0)


class TestRecord:
    def test_records_one_row_with_the_given_fields(self, conn: sqlite3.Connection) -> None:
        event_id = claim_events.record(
            conn, subject_table="commitment", subject_id=42, cause="resolved",
            field="status", old_value="open", new_value="done",
        )
        row = conn.execute(
            "SELECT * FROM claim_event WHERE id = ?", (event_id,)
        ).fetchone()
        assert row["subject_table"] == "commitment"
        assert row["subject_id"] == 42
        assert row["cause"] == "resolved"
        assert row["old_value"] == "open"
        assert row["new_value"] == "done"

    def test_since_returns_only_this_subjects_history_in_order(
        self, conn: sqlite3.Connection
    ) -> None:
        claim_events.record(conn, subject_table="commitment", subject_id=1, cause="a")
        first = claim_events.record(conn, subject_table="commitment", subject_id=2, cause="b")
        second = claim_events.record(conn, subject_table="commitment", subject_id=2, cause="c")
        claim_events.record(conn, subject_table="fact", subject_id=2, cause="d")

        history = claim_events.since(conn, "commitment", 2)
        assert [r["id"] for r in history] == [first, second]

    def test_since_after_id_bounds_a_diff(self, conn: sqlite3.Connection) -> None:
        first = claim_events.record(conn, subject_table="commitment", subject_id=9, cause="a")
        second = claim_events.record(conn, subject_table="commitment", subject_id=9, cause="b")

        assert [r["id"] for r in claim_events.since(conn, "commitment", 9, after_id=first)] == [
            second
        ]


class TestDependency:
    def test_depends_on_fact_then_active_dependencies_returns_it(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        fact_id = facts.remember(conn, settings, "housing", "dorm", "Willow Hall")
        claim_events.depends_on_fact(
            conn, subject_table="commitment", subject_id=7, fact_id=fact_id,
            quote="Willow Hall", reason="scholarship tied to this dorm",
        )
        deps = claim_events.active_dependencies(conn, "commitment", 7)
        assert len(deps) == 1
        assert deps[0].fact_id == fact_id
        assert deps[0].status == "active"

    def test_depends_on_none_is_first_class_not_a_default(
        self, conn: sqlite3.Connection
    ) -> None:
        claim_events.depends_on_none(
            conn, subject_table="commitment", subject_id=8, reason="no fact cited"
        )
        deps = claim_events.active_dependencies(conn, "commitment", 8)
        assert len(deps) == 1
        assert deps[0].kind == "none"
        assert deps[0].fact_id is None

    def test_recording_the_same_dependency_twice_does_not_duplicate(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        fact_id = facts.remember(conn, settings, "housing", "dorm", "Willow Hall")
        claim_events.depends_on_fact(
            conn, subject_table="commitment", subject_id=7, fact_id=fact_id,
            quote="a", reason="first",
        )
        claim_events.depends_on_fact(
            conn, subject_table="commitment", subject_id=7, fact_id=fact_id,
            quote="b", reason="second",
        )
        deps = claim_events.active_dependencies(conn, "commitment", 7)
        assert len(deps) == 1
        assert deps[0].reason == "second"

    def test_dependents_of_fact_finds_every_subject_naming_it(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        fact_id = facts.remember(conn, settings, "housing", "dorm", "Willow Hall")
        claim_events.depends_on_fact(
            conn, subject_table="commitment", subject_id=1, fact_id=fact_id,
            quote="a", reason="r1",
        )
        claim_events.depends_on_fact(
            conn, subject_table="commitment", subject_id=2, fact_id=fact_id,
            quote="b", reason="r2",
        )
        dependents = claim_events.dependents_of_fact(conn, fact_id)
        assert {d.subject_id for d in dependents} == {1, 2}


class TestInvalidateFact:
    def test_invalidate_fact_supersedes_every_dependent(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        fact_id = facts.remember(conn, settings, "housing", "dorm", "Willow Hall")
        claim_events.depends_on_fact(
            conn, subject_table="commitment", subject_id=1, fact_id=fact_id,
            quote="a", reason="r1",
        )
        event_id = claim_events.record(
            conn, subject_table="fact", subject_id=fact_id, cause="fact_superseded",
        )
        broken = claim_events.invalidate_fact(conn, fact_id, event_id=event_id)
        assert len(broken) == 1
        assert claim_events.active_dependencies(conn, "commitment", 1) == []
        row = conn.execute(
            "SELECT status, superseded_by FROM claim_dependency WHERE fact_id = ?",
            (fact_id,),
        ).fetchone()
        assert row["status"] == "superseded"
        assert row["superseded_by"] == event_id

    def test_invalidate_fact_with_no_dependents_is_a_no_op(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        fact_id = facts.remember(conn, settings, "housing", "dorm", "Willow Hall")
        event_id = claim_events.record(
            conn, subject_table="fact", subject_id=fact_id, cause="fact_superseded",
        )
        assert claim_events.invalidate_fact(conn, fact_id, event_id=event_id) == []


class TestFactsWiring:
    def test_remember_superseding_a_fact_emits_events_and_breaks_dependents(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        first = facts.remember(conn, settings, "housing", "dorm", "Willow Hall")
        claim_events.depends_on_fact(
            conn, subject_table="commitment", subject_id=3, fact_id=first,
            quote="Willow Hall", reason="scholarship tied to this dorm",
        )
        facts.remember(conn, settings, "housing", "dorm", "Sun Devil Village")

        history = claim_events.since(conn, "fact", first)
        assert any(r["cause"] == "fact_superseded" for r in history)
        assert claim_events.active_dependencies(conn, "commitment", 3) == []

    def test_remember_with_no_dependents_emits_exactly_one_event(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        first = facts.remember(conn, settings, "housing", "dorm", "Willow Hall")
        facts.remember(conn, settings, "housing", "dorm", "Sun Devil Village")
        history = claim_events.since(conn, "fact", first)
        assert len(history) == 1
        assert history[0]["cause"] == "fact_superseded"

    def test_forget_emits_retracted_event_and_invalidates(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        fact_id = facts.remember(conn, settings, "housing", "dorm", "Willow Hall")
        claim_events.depends_on_fact(
            conn, subject_table="commitment", subject_id=5, fact_id=fact_id,
            quote="a", reason="r",
        )
        facts.forget(conn, fact_id)
        history = claim_events.since(conn, "fact", fact_id)
        assert any(r["cause"] == "fact_retracted" for r in history)
        assert claim_events.active_dependencies(conn, "commitment", 5) == []


class TestDryRunWritesNothing:
    """`backglass sync --dry-run` promises zero writes. Found while wiring claim_events
    into facts.remember(): apply_extracted writes through a raw `conn`, not through the
    dry-run-aware `Ledger`, so a dry run was writing real fact (and now claim_event) rows
    the whole time. Drives the real door — a real sync() call, not the helper — because
    that is exactly the class of bug a unit test on facts.py alone cannot see.

    A FRESH ledger cannot exercise this: dry-run ingest never really inserts a
    `source_item` (Ledger returns a pseudo id), so `pending_extraction_unbatched` finds
    nothing and extraction never runs at all regardless of the bug. The real-world case
    is the owner's actual ledger, where a `keep`-triaged, unextracted item already
    exists from a prior real sync — so the item here is seeded directly, the shape
    `pending_extraction_unbatched.sql` and the live installation both already carry.
    """

    def test_dry_run_extraction_writes_no_facts_and_no_claim_events(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        from backglass.sync import sync
        from tests.conftest import FakeModel

        body = "Your housing assignment is confirmed: Willow Hall, Room 502."
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, author, title, body_text, raw_json, content_hash,"
            " triage_verdict) VALUES (?, 'gmail:personal', 'dryrun1', datetime('now'),"
            " '2026-08-07T09:00:00-07:00', 'housing@example.edu',"
            " 'Housing assignment confirmed', ?, '{}', 'h-dryrun1', 'keep')",
            (USER_ID, body),
        )
        model = FakeModel(
            extract={
                "Willow Hall": {
                    "commitments": [],
                    "engagements": [],
                    "facts": [{
                        "subject": "housing", "key": "dorm",
                        "value": "Willow Hall room 502",
                        "evidence": body,
                        "confidence": 0.95,
                    }],
                }
            }
        )
        sync(conn, settings, [], model, dry_run=True)
        assert model.calls, "the seeded item should have reached extraction"
        assert conn.execute("SELECT COUNT(*) AS n FROM fact").fetchone()["n"] == 0
        assert (
            conn.execute("SELECT COUNT(*) AS n FROM claim_event").fetchone()["n"] == 0
        )


class TestActionsWiring:
    def test_resolve_emits_a_claim_event(self, conn: sqlite3.Connection) -> None:
        cid = _open_commitment(conn)
        actions.resolve(conn, cid)
        history = claim_events.since(conn, "commitment", cid)
        assert len(history) == 1
        assert history[0]["cause"] == "resolved"
        assert history[0]["old_value"] == "open"
        assert history[0]["new_value"] == "done"

    def test_drop_emits_a_claim_event(self, conn: sqlite3.Connection) -> None:
        cid = _open_commitment(conn)
        actions.drop(conn, cid)
        history = claim_events.since(conn, "commitment", cid)
        assert len(history) == 1
        assert history[0]["cause"] == "dropped"
        assert history[0]["new_value"] == "dropped"
