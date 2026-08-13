"""Reach-out drafts: the email that keeps an in-person connection warm.

The cases that matter are the ones with no evidence behind them. Someone met once at a
dinner has no thread, no commitment and no last touch, and the draft still has to be a
complete, sendable email — that is the whole reason this exists rather than being a
report over the ledger.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from backglass.config import Settings
from backglass.facts import remember
from backglass.people import reachout

TODAY = date(2026, 8, 12)

NOTE = (
    "I especially appreciated you answering my questions about how immune cells tell "
    "cancerous cells apart from healthy ones."
)


def _person(
    conn: sqlite3.Connection,
    name: str,
    *,
    aliases: list[str] | None = None,
    role: str | None = None,
    org: str | None = None,
    tags: list[str] | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO entity (kind, canonical_name, aliases_json, role, org, tags_json)"
        " VALUES ('person', ?, ?, ?, ?, ?)",
        (name, json.dumps(aliases or []), role, org, json.dumps(tags or [])),
    )
    return int(cur.lastrowid)


def _interaction(conn: sqlite3.Connection, entity_id: int, occurred: str) -> int:
    """A commitment with a source item — what `people_cold.sql` reads as a touch."""
    cur = conn.execute(
        "INSERT INTO source_item (source, external_id, fetched_at, occurred_at, author,"
        " title, body_text, content_hash, triage_verdict)"
        " VALUES ('apple-mail', ?, ?, ?, 'them@example.com', 'Subject', 'body', ?, 'keep')",
        (f"m-{entity_id}-{occurred}", occurred, occurred, f"h-{entity_id}-{occurred}"),
    )
    sid = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO commitment (direction, counterparty_entity_id, what, confidence,"
        " source_item_id, created_at) VALUES ('i_owe', ?, 'thing', 0.9, ?, ?)",
        (entity_id, sid, occurred),
    )
    return sid


def _owner(conn: sqlite3.Connection, settings: Settings) -> None:
    remember(conn, settings, "identity", "name", "Dharsan Kesavan")
    remember(
        conn, settings, "identity", "emails",
        "contactdharsan@gmail.com (personal) · dkesava2@asu.edu (ASU)",
    )


class TestNoEvidence:
    """The primary case: met in person, nothing in the ledger."""

    def test_renders_a_complete_email_from_name_and_note(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _owner(conn, settings)
        pid = _person(conn, "Felipe Batalini", tags=["connection"])

        record = reachout.draft(
            conn, settings, pid, note=NOTE, template="thanks",
            where="at the McKenna dinner", day=TODAY,
        )

        assert record.body.startswith("Hi Felipe,")
        assert NOTE in record.body
        assert "at the McKenna dinner" in record.body
        assert record.subject == "Thank you for your time, Felipe"
        assert record.body.rstrip().endswith("Dharsan Kesavan\ncontactdharsan@gmail.com")
        # Nothing half-filled reaches the owner.
        assert "{" not in record.body and "}" not in record.body

    def test_no_touch_means_no_invented_gap(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        record = reachout.draft(conn, settings, pid, note=NOTE, template="warm", day=TODAY)

        assert "days" not in record.body
        assert "It's been a while since we last spoke and" in record.body
        assert any("gap: omitted" in line for line in record.evidence)

    def test_where_is_optional_and_leaves_no_seam(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        record = reachout.draft(conn, settings, pid, note=NOTE, day=TODAY)

        assert "Thank you for taking the time to talk. " + NOTE in record.body
        assert "  " not in record.body

    def test_missing_owner_facts_degrade_to_an_unsigned_draft(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """No identity fact must never mean an invented signature (rule 1)."""
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        record = reachout.draft(conn, settings, pid, note=NOTE, day=TODAY)

        assert record.body.rstrip().endswith("Best,")
        assert not any("sign-off" in line for line in record.evidence)


class TestLedgerEnrichment:
    def test_gap_states_the_measured_number_and_cites_the_row(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        pid = _person(conn, "Ravi Menon", role="Partner")
        sid = _interaction(conn, pid, "2026-06-13T10:00:00Z")

        record = reachout.draft(conn, settings, pid, note=NOTE, template="warm", day=TODAY)

        assert "about 60 days" in record.body
        assert any(f"source_item #{sid}" in line for line in record.evidence)

    def test_org_appears_only_when_the_profile_carries_one(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        with_org = _person(conn, "Ravi Menon", org="Stellar Capital", tags=["investor"])
        without = _person(conn, "Ana Silva", tags=["connection"])

        assert "at Stellar Capital" in reachout.draft(
            conn, settings, with_org, note=NOTE, template="ask", day=TODAY
        ).body
        assert " at " not in reachout.draft(
            conn, settings, without, note=NOTE, template="ask", day=TODAY
        ).body


class TestGuards:
    def test_a_draft_without_a_note_is_refused(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        with pytest.raises(reachout.ReachoutError, match="--note"):
            reachout.draft(conn, settings, pid, note="   ", day=TODAY)

    def test_unknown_template_names_the_known_ones(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        with pytest.raises(reachout.ReachoutError, match="thanks"):
            reachout.draft(conn, settings, pid, note=NOTE, template="cold-blast", day=TODAY)

    def test_every_template_survives_a_bare_profile(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        for name in reachout.TEMPLATES:
            record = reachout.draft(conn, settings, pid, note=NOTE, template=name, day=TODAY)
            assert "{" not in record.body and "{" not in record.subject
            assert NOTE in record.body
            assert record.subject.strip()

    def test_ambiguous_name_refuses_rather_than_guessing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        del settings
        _person(conn, "Felipe Batalini", tags=["connection"])
        _person(conn, "Felipe Moreno", tags=["connection"])
        with pytest.raises(reachout.ReachoutError, match="matches 2 people"):
            reachout.resolve(conn, "felipe")

    def test_exact_name_wins_over_a_partial_sibling(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        del settings
        pid = _person(conn, "Felipe", tags=["connection"])
        _person(conn, "Felipe Batalini", tags=["connection"])
        assert reachout.resolve(conn, "Felipe") == pid

    def test_drafting_writes_nothing(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """CLAUDE.md rule 3, in the form that matters here: a report is not a sync."""
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        conn.commit()
        before = conn.total_changes

        reachout.draft(conn, settings, pid, note=NOTE, day=TODAY)
        reachout.candidates(conn, settings, TODAY)

        assert conn.total_changes == before


class TestAddress:
    def test_no_alias_means_no_mailto_and_the_draft_says_so(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        record = reachout.draft(conn, settings, pid, note=NOTE, day=TODAY)

        assert record.to_email is None
        assert record.mailto() is None
        assert any("address: none on the profile" in line for line in record.evidence)

    def test_an_email_alias_becomes_a_mailto(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        pid = _person(
            conn, "Felipe Batalini", aliases=["felipe@example.edu"], tags=["connection"]
        )
        record = reachout.draft(conn, settings, pid, note=NOTE, day=TODAY)

        link = record.mailto() or ""
        assert link.startswith("mailto:felipe%40example.edu?subject=")
        assert "immune%20cells" in link


class TestCandidates:
    def test_a_never_touched_curated_profile_is_a_candidate(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """`touch.needing_follow_up` requires a source row, so it cannot see the person
        this feature is for. The candidate list must."""
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        assert [t.entity_id for t in reachout.candidates(conn, settings, TODAY)] == [pid]

    def test_an_uncurated_profile_is_not_tracked(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _person(conn, "Someone From Extraction")
        assert reachout.candidates(conn, settings, TODAY) == []


class TestPersonPage:
    """The same draft on the surface the owner actually opens."""

    @pytest.fixture
    def client(self, conn: sqlite3.Connection, settings: Settings):  # type: ignore[no-untyped-def]
        from fastapi.testclient import TestClient

        from backglass.web.app import create_app

        del conn  # migrated db on disk; the app opens its own connections
        return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")

    def test_the_panel_offers_every_template(
        self, conn: sqlite3.Connection, settings: Settings, client
    ) -> None:  # type: ignore[no-untyped-def]
        del settings
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        conn.commit()

        page = client.get(f"/people/{pid}")

        assert page.status_code == 200
        for name in reachout.TEMPLATES:
            assert f'value="{name}"' in page.text

    def test_posting_a_note_returns_the_draft_with_its_provenance(
        self, conn: sqlite3.Connection, settings: Settings, client
    ) -> None:  # type: ignore[no-untyped-def]
        _owner(conn, settings)
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        conn.commit()
        before = conn.total_changes

        page = client.post(
            f"/people/{pid}/reachout",
            data={"template": "thanks", "note": NOTE, "where": "at the McKenna dinner"},
        )

        assert page.status_code == 200
        assert "Hi Felipe," in page.text
        assert "immune cells" in page.text
        assert "Where each part came from" in page.text
        assert conn.total_changes == before  # a draft is a read

    def test_an_empty_note_answers_in_the_fragment_not_a_500(
        self, conn: sqlite3.Connection, settings: Settings, client
    ) -> None:  # type: ignore[no-untyped-def]
        del settings
        pid = _person(conn, "Felipe Batalini", tags=["connection"])
        conn.commit()

        page = client.post(f"/people/{pid}/reachout", data={"template": "thanks", "note": ""})

        assert page.status_code == 200
        assert "--note" in page.text
        assert "Hi Felipe," not in page.text
