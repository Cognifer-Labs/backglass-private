"""Template dedup: hash stability, the K-drop rule, and the one-keep disqualifier."""

from __future__ import annotations

import sqlite3

from backglass.config import Settings
from backglass.extract import templates
from backglass.sync import sync
from tests.conftest import FakeModel, gmail_message, make_connector

STATEMENT_A = (
    "Your statement for July 2026 is ready. Balance: $1,234.56. "
    "View it at https://bank.example/statements/8842 before 08/15/2026."
)
STATEMENT_B = (
    "Your statement for August 2026 is ready. Balance: $88.20. "
    "View it at https://bank.example/statements/9911 before 09/15/2026."
)


class TestHash:
    def test_variable_parts_do_not_change_the_hash(self) -> None:
        a = templates.template_hash(
            author="Bank <alerts@bank.example>", title="Statement 8842", body_text=STATEMENT_A
        )
        b = templates.template_hash(
            author="alerts@bank.example", title="Statement 9911", body_text=STATEMENT_B
        )
        assert a is not None and a == b

    def test_different_prose_or_domain_changes_the_hash(self) -> None:
        base = templates.template_hash(
            author="alerts@bank.example", title="Statement", body_text=STATEMENT_A
        )
        other_prose = templates.template_hash(
            author="alerts@bank.example",
            title="Statement",
            body_text="Your account is overdrawn. Contact us immediately.",
        )
        other_domain = templates.template_hash(
            author="alerts@other.example", title="Statement", body_text=STATEMENT_A
        )
        assert base != other_prose
        assert base != other_domain

    def test_non_mail_items_have_no_template(self) -> None:
        assert templates.template_hash(author="anki", title="t", body_text="b") is None
        assert templates.template_hash(author=None, title="t", body_text="b") is None


def _statement_message(i: int, body: str) -> dict[str, object]:
    return {
        "id": f"stmt{i}",
        "from": "alerts@bank.example",
        "to": "contactdharsan@gmail.com",
        "subject": f"Statement #{i}",
        "date": f"Fri, {10 + i:02d} Jul 2026 09:00:00 -0700",
        "body": body,
    }


def _seed_dropped_siblings(
    conn: sqlite3.Connection, settings: Settings, boundary, n: int, verdict: str = "drop"
) -> FakeModel:
    """Run real syncs so the siblings carry real template hashes, then force verdicts."""
    messages = [
        gmail_message(_statement_message(i, STATEMENT_A.replace("1,234.56", f"{i}.00")))
        for i in range(n)
    ]
    model = FakeModel(
        triage={"Statement": {"keep": False, "reason": "recurring statement, no ask"}}
    )
    sync(conn, settings, [make_connector(messages, boundary)], model)
    if verdict != "drop":
        conn.execute(
            "UPDATE source_item SET triage_verdict = ?, triage_reason = 'forced'"
            " WHERE external_id = 'stmt0'",
            (verdict,),
        )
    return model


class TestRule:
    def test_fourth_sibling_is_dropped_free_after_three_paid_drops(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        model = _seed_dropped_siblings(conn, settings, boundary, 3)
        calls_before = len(model.calls)
        assert calls_before == 3, "the three siblings were paid model drops"

        fourth = gmail_message(_statement_message(9, STATEMENT_B))
        report = sync(conn, settings, [make_connector([fourth], boundary)], model)

        assert report.rule_dropped == 1
        assert len(model.calls) == calls_before, "the fourth sibling costs nothing"
        reason = conn.execute(
            "SELECT triage_reason FROM source_item WHERE external_id = 'stmt9'"
        ).fetchone()["triage_reason"]
        assert reason.startswith("template:")

    def test_one_kept_sibling_disqualifies_the_template(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        model = _seed_dropped_siblings(conn, settings, boundary, 4, verdict="keep")
        triage_before = sum(1 for tier, _ in model.calls if tier == "triage")

        fifth = gmail_message(_statement_message(9, STATEMENT_B))
        report = sync(conn, settings, [make_connector([fifth], boundary)], model)

        assert report.rule_dropped == 0
        triage_after = sum(1 for tier, _ in model.calls if tier == "triage")
        assert triage_after == triage_before + 1, "the keep forces a model read"

    def test_idempotent_second_run(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        model = _seed_dropped_siblings(conn, settings, boundary, 3)
        fourth = gmail_message(_statement_message(9, STATEMENT_B))
        sync(conn, settings, [make_connector([fourth], boundary)], model)
        calls = len(model.calls)

        second = sync(conn, settings, [make_connector([fourth], boundary)], model)
        assert second.writes == 0
        assert len(model.calls) == calls


class TestBackfill:
    def test_backfill_fills_only_null_hashes_and_is_idempotent(
        self, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, author, title, body_text, raw_json, content_hash)"
            " VALUES (1, 'gmail:t', 'old1', '2026-07-01T00:00:00+00:00',"
            " '2026-07-01T00:00:00+00:00', 'alerts@bank.example', 'Statement', ?, '{}',"
            " 'h:old1')",
            (STATEMENT_A,),
        )
        from backglass.extract import templates as templates_mod

        rows = list(
            conn.execute(
                "SELECT id, author, title, body_text FROM source_item"
                " WHERE template_hash IS NULL"
            )
        )
        assert len(rows) == 1
        for r in rows:
            th = templates_mod.template_hash(
                author=r["author"], title=r["title"], body_text=r["body_text"]
            )
            if th:
                conn.execute(
                    "UPDATE source_item SET template_hash = ? WHERE id = ?", (th, r["id"])
                )

        again = list(
            conn.execute(
                "SELECT id FROM source_item WHERE template_hash IS NULL AND author LIKE '%@%'"
            )
        )
        assert again == []

    def test_backfill_update_passes_the_immutability_trigger(
        self, conn: sqlite3.Connection
    ) -> None:
        """template_hash is deliberately outside 0002's protected columns."""
        conn.execute(
            "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
            " occurred_at, author, content_hash)"
            " VALUES (1, 'gmail:t', 'trig', '2026-07-01T00:00:00+00:00',"
            " '2026-07-01T00:00:00+00:00', 'a@b.c', 'h:trig')"
        )
        conn.execute(
            "UPDATE source_item SET template_hash = 'abc' WHERE external_id = 'trig'"
        )  # must not raise
