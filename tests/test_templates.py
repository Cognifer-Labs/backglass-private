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
        "to": "alex.rivera@example.com",
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


BULK_STATEMENT = (
    "Apply now — your scholarship match closes 08/15/2026. "
    "See https://scholar.example/offers/8842 . Unsubscribe to stop these."
)


def _settle(
    conn: sqlite3.Connection, external_ids: list[str], *, produced: bool = False
) -> None:
    """Mark siblings the way the extract pass would leave them: kept, run to completion,
    and either productive or barren.

    The stamp is the REAL prompt version, not a placeholder. `pending_extraction` selects
    on `extraction_version != <current stamp>`, so a made-up version means "extracted
    under an older prompt" and every sibling comes back as pending — which is not the
    state being set up, and quietly turns the assertion about call counts into an
    assertion about re-extraction.
    """
    from backglass.extract import prompts

    stamp = prompts.load("extract-commitments").stamp
    for ext in external_ids:
        conn.execute(
            "UPDATE source_item SET triage_verdict = 'keep', triage_reason = 'forced',"
            " extraction_version = ? WHERE external_id = ?",
            (stamp, ext),
        )
        # The seeding sync really extracts, and the fake really returns a commitment.
        # Barren means the pass ran and found NOTHING, so the records it invented are
        # cleared — otherwise every sibling is productive and the rule can never fire.
        row = conn.execute(
            "SELECT id FROM source_item WHERE external_id = ?", (ext,)
        ).fetchone()
        conn.execute(
            "DELETE FROM commitment_evidence WHERE source_item_id = ?", (row["id"],)
        )
        conn.execute("DELETE FROM commitment WHERE source_item_id = ?", (row["id"],))
        if produced:
            conn.execute(
                "INSERT INTO commitment (user_id, direction, what, status, confidence,"
                " source_item_id, created_at) VALUES (1, 'i_owe', 'a real ask', 'open',"
                " 0.9, ?, '2026-08-07T00:00:00Z')",
                (row["id"],),
            )


class TestSettledEvidence:
    """A keep the expensive pass proved empty is evidence about the SHAPE too.

    The sender rule learned this on 2026-08-05; templates carried the same defect for
    the same reason. "One keep, ever, disqualifies" is the right instinct aimed at the
    wrong signal: marketing mail is kept precisely because it is written to look like a
    deadline, so the shapes costing the most could never qualify and their siblings were
    re-triaged and re-extracted forever.
    """

    def _bulk_siblings(self, conn, settings, boundary, n: int) -> FakeModel:  # type: ignore[no-untyped-def]
        messages = [
            gmail_message(_statement_message(i, BULK_STATEMENT.replace("8842", f"{i}00")))
            for i in range(n)
        ]
        model = FakeModel(triage={"Statement": {"keep": True, "reason": "has a deadline"}})
        sync(conn, settings, [make_connector(messages, boundary)], model)
        return model

    def test_barren_bulk_siblings_earn_the_free_drop(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        model = self._bulk_siblings(conn, settings, boundary, 3)
        _settle(conn, [f"stmt{i}" for i in range(3)])
        before = len(model.calls)

        fourth = gmail_message(_statement_message(9, BULK_STATEMENT.replace("8842", "999")))
        report = sync(conn, settings, [make_connector([fourth], boundary)], model)

        assert report.rule_dropped == 1
        assert len(model.calls) == before, "the fourth sibling costs nothing"

    def test_one_productive_sibling_disqualifies_the_shape_forever(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        """Volume never outvotes a real extraction. Three barren siblings and one that
        yielded a commitment is a shape that sometimes matters."""
        model = self._bulk_siblings(conn, settings, boundary, 4)
        _settle(conn, [f"stmt{i}" for i in range(3)])
        _settle(conn, ["stmt3"], produced=True)
        before = len(model.calls)

        fifth = gmail_message(_statement_message(9, BULK_STATEMENT.replace("8842", "999")))
        report = sync(conn, settings, [make_connector([fifth], boundary)], model)

        assert report.rule_dropped == 0
        assert len(model.calls) > before, "the productive sibling forces a model read"

    def test_an_unanswered_sibling_disqualifies_the_shape(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        """Kept and not yet extracted is an open question, not a resolved nothing — the
        next extract pass may still turn it into a commitment."""
        model = self._bulk_siblings(conn, settings, boundary, 4)
        _settle(conn, [f"stmt{i}" for i in range(3)])
        conn.execute(
            "UPDATE source_item SET triage_verdict = 'keep', extraction_version = NULL"
            " WHERE external_id = 'stmt3'"
        )
        conn.execute(
            "DELETE FROM commitment WHERE source_item_id ="
            " (SELECT id FROM source_item WHERE external_id = 'stmt3')"
        )
        before = len(model.calls)

        fifth = gmail_message(_statement_message(9, BULK_STATEMENT.replace("8842", "999")))
        report = sync(conn, settings, [make_connector([fifth], boundary)], model)

        assert report.rule_dropped == 0
        assert len(model.calls) > before

    def test_a_barren_keep_without_a_broadcast_marker_is_not_evidence(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        """The guard that keeps this class away from human correspondents. A colleague
        who writes the same shape of note repeatedly accumulates barren keeps too, and
        personal mail carries no unsubscribe footer."""
        messages = [
            gmail_message(_statement_message(i, STATEMENT_A.replace("1,234.56", f"{i}.00")))
            for i in range(3)
        ]
        model = FakeModel(triage={"Statement": {"keep": True, "reason": "might be an ask"}})
        sync(conn, settings, [make_connector(messages, boundary)], model)
        _settle(conn, [f"stmt{i}" for i in range(3)])
        before = len(model.calls)

        fourth = gmail_message(_statement_message(9, STATEMENT_A.replace("1,234.56", "9.00")))
        report = sync(conn, settings, [make_connector([fourth], boundary)], model)

        assert report.rule_dropped == 0
        assert len(model.calls) > before, "no marker, no free drop"
