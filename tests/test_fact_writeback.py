"""The pipeline writes the knowledge base — through the poison gate.

An extracted fact feeds owner_context, which feeds every future model call, so unlike a
wrong commitment a wrong fact compounds. Everything here is about the gate: what may
land active on its own, what must wait as proposed, and the guarantee that proposed
rows are invisible to every prompt until the owner accepts them.
"""

from __future__ import annotations

import sqlite3

import pytest

from backglass import facts
from backglass.config import Settings
from backglass.extract.schemas import ExtractedFact

BODY = (
    "Congratulations! Your housing assignment is confirmed: Willow Hall, Room 502. "
    "Move-in begins August 20th."
)


def _candidate(**over: object) -> ExtractedFact:
    base: dict[str, object] = {
        "subject": "housing",
        "key": "dorm",
        "value": "Willow Hall room 502 for fall 2026",
        "evidence": "Your housing assignment is confirmed: Willow Hall, Room 502.",
        "confidence": 0.95,
    }
    base.update(over)
    return ExtractedFact.model_validate(base)


def _item(conn: sqlite3.Connection, external_id: str = "m1") -> int:
    cur = conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " title, body_text, content_hash, triage_verdict)"
        " VALUES (1, 'manual', ?, '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z',"
        " 'T', ?, ?, 'keep')",
        (external_id, BODY, f"h-{external_id}"),
    )
    return int(cur.lastrowid)


def _apply(
    conn: sqlite3.Connection, settings: Settings, *cands: ExtractedFact, body: str = BODY
) -> int:
    return facts.apply_extracted(
        conn, settings, list(cands), source_item_id=_item(conn, f"m{conn.total_changes}"),
        body_text=body,
    )


class TestTheGate:
    def test_a_cited_confident_fact_lands_active_with_provenance(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _apply(conn, settings, _candidate())

        row = conn.execute("SELECT * FROM fact WHERE status = 'active'").fetchone()
        assert row["subject"] == "housing" and row["key"] == "dorm"
        assert row["source"] == "extraction"
        assert row["source_item_id"] is not None
        assert row["confidence"] == pytest.approx(0.95)
        # And it is now part of what every model call carries.
        assert "Willow Hall room 502" in facts.owner_context(conn)

    def test_a_hesitant_fact_waits_as_proposed(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _apply(conn, settings, _candidate(confidence=0.5))

        assert conn.execute("SELECT COUNT(*) AS n FROM fact WHERE status = 'active'"
                            ).fetchone()["n"] == 0
        assert len(facts.proposed(conn)) == 1

    def test_an_uncitable_quote_waits_as_proposed_however_confident(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """A model that cannot quote the sentence it read does not get to write
        memory — rule 1 at the gate, the same rule recheck applies to closes."""
        _apply(conn, settings, _candidate(evidence="You have been assigned Maple Hall."))

        assert len(facts.proposed(conn)) == 1
        assert facts.owner_context(conn) == ""

    def test_the_citation_check_forgives_whitespace_and_case_only(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _apply(conn, settings, _candidate(
            evidence="your housing  assignment is\nconfirmed: willow hall, room 502."
        ))
        assert conn.execute("SELECT COUNT(*) AS n FROM fact WHERE status = 'active'"
                            ).fetchone()["n"] == 1

    def test_an_invented_lane_waits_as_proposed(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _apply(conn, settings, _candidate(subject="dorm-assignments"))
        assert len(facts.proposed(conn)) == 1
        assert facts.owner_context(conn) == ""

    def test_a_proposed_fact_is_invisible_to_owner_context(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The load-bearing guarantee: nothing unreviewed reaches a prompt."""
        _apply(conn, settings, _candidate(confidence=0.3))
        assert facts.owner_context(conn) == ""
        assert facts.recall(conn) == []


class TestIdempotency:
    def test_re_emitting_the_active_value_writes_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Rule 3: a re-extraction that says what the ledger says leaves no row."""
        _apply(conn, settings, _candidate())
        before = conn.execute("SELECT COUNT(*) AS n FROM fact").fetchone()["n"]

        wrote = _apply(conn, settings, _candidate())
        assert wrote == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM fact").fetchone()["n"] == before

    def test_re_proposing_the_same_pending_value_writes_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _apply(conn, settings, _candidate(confidence=0.4))
        wrote = _apply(conn, settings, _candidate(confidence=0.4))
        assert wrote == 0
        assert len(facts.proposed(conn)) == 1

    def test_a_changed_value_supersedes_the_old_active_fact(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _apply(conn, settings, _candidate(value="Willow Hall room 502 for fall 2026"))
        body2 = "Update: your room has changed. You are now in Willow Hall, Room 610."
        facts.apply_extracted(
            conn, settings,
            [_candidate(value="Willow Hall room 610 for fall 2026",
                        evidence="You are now in Willow Hall, Room 610.")],
            source_item_id=_item(conn, "m-update"), body_text=body2,
        )

        active = conn.execute(
            "SELECT value FROM fact WHERE status = 'active' AND key = 'dorm'"
        ).fetchall()
        assert [r["value"] for r in active] == ["Willow Hall room 610 for fall 2026"]
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM fact WHERE status = 'superseded'"
        ).fetchone()["n"] == 1


class TestTheReview:
    def test_accept_makes_it_active_through_supersession(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        facts.remember(conn, settings, "housing", "dorm", "old dorm")
        _apply(conn, settings, _candidate(confidence=0.4))
        pid = facts.proposed(conn)[0].fact_id

        facts.accept(conn, settings, pid)

        active = conn.execute(
            "SELECT value, source, confidence FROM fact"
            " WHERE status = 'active' AND key = 'dorm'"
        ).fetchall()
        assert len(active) == 1
        assert active[0]["value"] == "Willow Hall room 502 for fall 2026"
        assert active[0]["source"] == "extraction"
        assert facts.proposed(conn) == []
        # Accepting twice is inert, not a duplicate.
        with pytest.raises(facts.FactError):
            facts.accept(conn, settings, pid)

    def test_reject_retracts_and_the_value_may_return_later(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _apply(conn, settings, _candidate(confidence=0.4))
        pid = facts.proposed(conn)[0].fact_id
        facts.reject(conn, pid)

        assert facts.proposed(conn) == []
        assert facts.owner_context(conn) == ""
        # A later message restating it proposes it again — June's wrong is
        # September's true.
        wrote = _apply(conn, settings, _candidate(confidence=0.4))
        assert wrote == 1
        assert len(facts.proposed(conn)) == 1


class TestTheMemoryPageDoor:
    """Drive the real door (2026-08-02 lesson: a guard on one of two doors is not a
    guard) — the web accept/reject must behave exactly as the CLI path."""

    @pytest.fixture
    def client(self, conn: sqlite3.Connection, settings: Settings):  # type: ignore[no-untyped-def]
        from fastapi.testclient import TestClient

        from backglass.web.app import create_app

        del conn
        return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")

    def test_proposed_facts_render_and_accept_moves_them_into_memory(
        self, conn: sqlite3.Connection, settings: Settings, client  # type: ignore[no-untyped-def]
    ) -> None:
        _apply(conn, settings, _candidate(confidence=0.4))
        conn.commit()
        pid = facts.proposed(conn)[0].fact_id

        page = client.get("/memory").text
        assert "proposed by extraction" in page
        assert "Willow Hall room 502" in page

        accepted = client.post(f"/memory/{pid}/accept")
        assert accepted.status_code == 200
        assert "proposed by extraction" not in accepted.text  # queue emptied
        assert len(facts.recall(conn)) == 1

    def test_reject_clears_the_queue_without_touching_memory(
        self, conn: sqlite3.Connection, settings: Settings, client  # type: ignore[no-untyped-def]
    ) -> None:
        _apply(conn, settings, _candidate(confidence=0.4))
        conn.commit()
        pid = facts.proposed(conn)[0].fact_id

        rejected = client.post(f"/memory/{pid}/reject")
        assert rejected.status_code == 200
        assert facts.recall(conn) == []
        assert facts.proposed(conn) == []
        # A row that is not proposed 404s rather than double-acting.
        assert client.post(f"/memory/{pid}/reject").status_code == 404
        assert client.post(f"/memory/{pid}/accept").status_code == 404


class TestTheWiring:
    def test_sync_apply_path_writes_facts_in_the_item_transaction(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Drive the same door sync drives: a CommitmentExtraction whose facts arrive
        beside its commitments, through facts.apply_extracted as _extract_pass calls
        it."""
        from backglass.extract.schemas import CommitmentExtraction

        extraction = CommitmentExtraction.model_validate({
            "commitments": [], "engagements": [],
            "facts": [{
                "subject": "housing", "key": "dorm",
                "value": "Willow Hall room 502 for fall 2026",
                "evidence": "Your housing assignment is confirmed: Willow Hall, Room 502.",
                "confidence": 0.95,
            }],
        })
        sid = _item(conn)
        facts.apply_extracted(
            conn, settings, extraction.facts, source_item_id=sid, body_text=BODY
        )
        row = conn.execute("SELECT source_item_id FROM fact WHERE status='active'").fetchone()
        assert row["source_item_id"] == sid

    def test_a_v9_response_with_no_facts_field_still_parses(self) -> None:
        """Every stored fixture and overnight batch reply predates v10; the default
        keeps them parseable, which is what `compatible:` promises."""
        from backglass.extract.schemas import CommitmentExtraction

        parsed = CommitmentExtraction.model_validate({"commitments": [], "engagements": []})
        assert parsed.facts == []
