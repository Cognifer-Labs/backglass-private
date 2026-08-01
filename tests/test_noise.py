"""Learned noise: mining, promotion, consumption, undo, and the idempotency contract.

The evidence bar under test is the load-bearing one: model drops only (rule drops
prove nothing new), zero keeps across all history, no commitment ever extracted.
"""

from __future__ import annotations

import sqlite3

from backglass.config import Settings
from backglass.extract import noise
from backglass.sync import sync
from tests.conftest import FakeModel, gmail_message, make_connector


def _triaged_item(
    conn: sqlite3.Connection,
    author: str,
    external_id: str,
    verdict: str,
    reason: str,
    occurred_at: str = "2026-07-10T09:00:00+00:00",
) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash, triage_verdict, triage_reason)"
        " VALUES (1, 'gmail:t', ?, ?, ?, ?, 't', 'b', '{}', ?, ?, ?)",
        (external_id, occurred_at, occurred_at, author, f"h:{external_id}", verdict, reason),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _seed_sender(
    conn: sqlite3.Connection,
    address: str,
    *,
    model_drops: int = 0,
    rule_drops: int = 0,
    keeps: int = 0,
) -> None:
    for i in range(model_drops):
        _triaged_item(conn, address, f"{address}:md{i}", "drop", "no ask, no deadline")
    for i in range(rule_drops):
        _triaged_item(conn, address, f"{address}:rd{i}", "drop", "bulk header: list-id")
    for i in range(keeps):
        _triaged_item(conn, address, f"{address}:k{i}", "keep", "contains a deadline")


class TestMining:
    def test_only_pure_model_drop_senders_qualify(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _seed_sender(conn, "spam@x.com", model_drops=5)
        _seed_sender(conn, "mixed@y.com", model_drops=5, keeps=1)
        _seed_sender(conn, "bulk@z.com", rule_drops=5)
        _seed_sender(conn, "thin@w.com", model_drops=4)

        found = noise.candidates(conn, settings, min_evidence=5)

        assert [c.value for c in found] == ["spam@x.com"]
        assert found[0].kind == "address"
        assert found[0].evidence_count == 5
        assert found[0].first_seen is not None

    def test_extracted_commitment_disqualifies_even_without_a_keep_row(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Belt-and-braces: the commitment ledger is asked directly."""
        _seed_sender(conn, "weird@x.com", model_drops=5)
        item = _triaged_item(conn, "weird@x.com", "weird:extra", "drop", "noise")
        conn.execute(
            "INSERT INTO commitment (user_id, direction, what, confidence, status,"
            " source_item_id, created_at)"
            " VALUES (1, 'i_owe', 'x', 0.9, 'open', ?, '2026-07-10T00:00:00')",
            (item,),
        )
        assert noise.candidates(conn, settings, min_evidence=5) == []

    def test_owner_and_configured_noise_are_never_candidates(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        owner = settings.model_copy(
            update={"owner_emails": ["me@self.com"], "noise_senders": ["covered.com"]}
        )
        _seed_sender(conn, "me@self.com", model_drops=6)
        _seed_sender(conn, "news@covered.com", model_drops=6)
        assert noise.candidates(conn, owner, min_evidence=5) == []

    def test_three_qualifying_addresses_surface_their_domain(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        for local in ("a", "b", "c"):
            _seed_sender(conn, f"{local}@blast.example", model_drops=5)
        found = noise.candidates(conn, settings, min_evidence=5)
        domains = [c for c in found if c.kind == "domain"]
        assert [d.value for d in domains] == ["blast.example"]


class TestPromotionAndConsumption:
    def test_promoted_sender_is_rule_dropped_with_zero_model_calls(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        _seed_sender(conn, "spam@x.com", model_drops=5)
        found = noise.candidates(conn, settings, min_evidence=5)
        assert noise.promote(conn, found, by="cli") == 1
        assert noise.promote(conn, found, by="cli") == 0  # idempotent

        fresh = gmail_message(
            {
                "id": "n1",
                "from": "spam@x.com",
                "to": "contactdharsan@gmail.com",
                "subject": "one more offer",
                "date": "Fri, 31 Jul 2026 09:00:00 -0700",
                "body": "Buy now.",
            }
        )
        model = FakeModel({})
        report = sync(conn, settings, [make_connector([fresh], boundary)], model)

        assert report.rule_dropped == 1
        assert model.calls == []
        reason = conn.execute(
            "SELECT triage_reason FROM source_item WHERE external_id = 'n1'"
        ).fetchone()["triage_reason"]
        assert "known-noise" in reason

    def test_auto_promote_is_idempotent_across_runs(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        _seed_sender(conn, "spam@x.com", model_drops=5)
        auto = settings.model_copy(update={"noise_auto_promote": True})
        model = FakeModel({})

        first = sync(conn, auto, [make_connector([], boundary)], model)
        assert first.writes == 1, "the promotion is the only write"
        assert noise.enabled_entries(conn) == frozenset({"spam@x.com"})

        second = sync(conn, auto, [make_connector([], boundary)], model)
        assert second.writes == 0


class TestUndo:
    def test_disabled_rule_stops_matching_and_requeue_retriages(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _seed_sender(conn, "spam@x.com", model_drops=5)
        noise.promote(conn, noise.candidates(conn, settings, 5), by="cli")
        _triaged_item(
            conn, "spam@x.com", "spam:rulehit", "drop", "known-noise sender: spam@x.com"
        )

        conn.execute(
            "UPDATE learned_noise SET enabled = 0 WHERE value = 'spam@x.com'"
        )
        assert noise.enabled_entries(conn) == frozenset()

        requeued = conn.execute(
            "UPDATE source_item SET triage_verdict = NULL, triage_reason = NULL"
            " WHERE user_id = 1 AND triage_reason IN (?, ?)",
            ("known-noise sender: spam@x.com", "known-noise domain: spam@x.com"),
        ).rowcount
        assert requeued == 1
        pending = conn.execute(
            "SELECT triage_verdict FROM source_item WHERE external_id = 'spam:rulehit'"
        ).fetchone()
        assert pending["triage_verdict"] is None
