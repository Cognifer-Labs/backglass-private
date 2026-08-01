"""Batch mode: submit packages exactly the right items; collect applies through the
same ledger paths sync uses — dedup, supersession, provenance, idempotency intact.

FakeBatches stands in for the Anthropic SDK; no live API (docs/10 §Testing).
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any

from backglass import batch as batch_mod
from backglass.config import Settings
from backglass.sync import sync
from tests.conftest import FakeModel, gmail_message, make_connector

EXTRACTION = {
    "commitments": [
        {
            "direction": "owed_to_me",
            "counterparty": "Dana Whitfield <dwhitfield@example.gov>",
            "what": "revised migration plan",
            "due_at": "2026-07-17",
            "due_is_explicit": True,
            "estimated_minutes": None,
            "confidence": 0.92,
            "evidence": "I'll have the revised migration plan over to you by Friday.",
            "resolves": False,
            "resolves_what": None,
        }
    ]
}


def _usage(input_tokens: int = 10_000, output_tokens: int = 500) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


def _succeeded(custom_id: str, data: dict[str, Any]) -> SimpleNamespace:
    message = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name="emit", input=data)],
        usage=_usage(),
    )
    return SimpleNamespace(
        custom_id=custom_id, result=SimpleNamespace(type="succeeded", message=message)
    )


def _errored(custom_id: str) -> SimpleNamespace:
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="errored"))


class FakeBatches:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.status = "ended"
        self.results_by_batch: dict[str, list[Any]] = {}
        self._counter = 0
        self.messages = SimpleNamespace(
            batches=SimpleNamespace(
                create=self._create, retrieve=self._retrieve, results=self._results
            )
        )

    def _create(self, *, requests: list[dict[str, Any]]) -> SimpleNamespace:
        self._counter += 1
        batch_id = f"msgbatch_{self._counter:03d}"
        self.created.append({"id": batch_id, "requests": requests})
        return SimpleNamespace(id=batch_id, processing_status="in_progress")

    def _retrieve(self, batch_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=batch_id, processing_status=self.status)

    def _results(self, batch_id: str):  # type: ignore[no-untyped-def]
        yield from self.results_by_batch.get(batch_id, [])


def _mail(i: int, body: str, sender: str = "dwhitfield@example.gov") -> dict[str, Any]:
    return {
        "id": f"bm{i}",
        "from": f"Dana Whitfield <{sender}>",
        "to": "contactdharsan@gmail.com",
        "subject": f"Plan {i}",
        "date": f"Fri, {10 + i:02d} Jul 2026 09:15:00 -0700",
        "body": body,
    }


def _submit(
    conn: sqlite3.Connection,
    settings: Settings,
    boundary,  # type: ignore[no-untyped-def]
    messages: list[dict[str, Any]],
    fake: FakeBatches,
) -> batch_mod.SubmitReport:
    model = FakeModel()  # default triage keeps everything
    return batch_mod.submit(
        conn,
        settings,
        [make_connector([gmail_message(m) for m in messages], boundary)],
        model,
        anthropic_client=fake,
    )


class TestSubmit:
    def test_packages_exactly_the_kept_unstamped_items(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        fake = FakeBatches()
        report = _submit(
            conn, settings, boundary,
            [_mail(1, "I'll send the plan by Friday."),
             _mail(2, "Deciding on the vendor next week.")],
            fake,
        )

        assert report.batched == 2
        assert report.batch_id == "msgbatch_001"
        requests = fake.created[0]["requests"]
        assert {r["custom_id"] for r in requests} == {"si-1", "si-2"}
        assert requests[0]["params"]["tool_choice"]["name"] == "emit"
        rows = list(conn.execute("SELECT custom_id, source_item_id FROM model_batch_item"))
        assert {(r["custom_id"], r["source_item_id"]) for r in rows} == {
            ("si-1", 1), ("si-2", 2),
        }

    def test_oversize_items_are_skipped_and_cap_blocks_submission(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        fake = FakeBatches()
        report = _submit(
            conn, settings, boundary, [_mail(1, "plan. " + "x" * 70_000)], fake
        )
        assert report.skipped_oversize == 1
        assert report.batched == 0
        assert fake.created == []

        capped = settings.model_copy(update={"monthly_spend_cap_cents": 0})
        fake2 = FakeBatches()
        report2 = _submit(conn, capped, boundary, [_mail(2, "Send it Friday.")], fake2)
        assert report2.batched == 0
        assert fake2.created == []
        assert any("spend cap" in e for e in report2.errors)

    def test_sync_skips_items_riding_a_live_batch(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        """The double-spend guard: pending_extraction_unbatched excludes them."""
        fake = FakeBatches()
        _submit(conn, settings, boundary, [_mail(1, "I'll send the plan Friday.")], fake)

        model = FakeModel({"Plan 1": EXTRACTION})
        report = sync(conn, settings, [make_connector([], boundary)], model)
        assert report.extracted == 0, "item is in a live batch; sync must not pay again"

        # Kill the batch → the item reappears in sync's pending set.
        conn.execute("UPDATE model_batch SET status = 'expired'")
        report2 = sync(conn, settings, [make_connector([], boundary)], model)
        assert report2.extracted == 1, "expired batch → synchronous fallback"


class TestCollect:
    def test_collect_applies_results_and_records_a_run(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        fake = FakeBatches()
        _submit(conn, settings, boundary, [_mail(1, "I'll send the plan Friday.")], fake)
        fake.results_by_batch["msgbatch_001"] = [_succeeded("si-1", EXTRACTION)]

        report = batch_mod.collect(conn, settings, anthropic_client=fake)

        assert report.batches == 1
        assert report.extracted == 1
        assert report.failed_items == 0
        # sonnet-4-6 at −50%: (10k×$3 + 500×$15)/1e6 × 0.5 = $0.01875 → 2c
        assert report.spend_cents == 2
        n = conn.execute("SELECT COUNT(*) AS n FROM commitment").fetchone()["n"]
        assert n == 1
        stamped = conn.execute(
            "SELECT extraction_version FROM source_item WHERE id = 1"
        ).fetchone()["extraction_version"]
        assert stamped and "@" in stamped
        run = conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT 1").fetchone()
        assert run["items_extracted"] == 1
        assert run["spend_cents"] == 2
        status = conn.execute("SELECT status FROM model_batch").fetchone()["status"]
        assert status == "collected"

    def test_two_batches_in_one_collect_record_their_own_spend(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        """The verifier's find: per-batch spend_cents must not accumulate across
        batches collected in the same call."""
        fake = FakeBatches()
        _submit(conn, settings, boundary, [_mail(1, "I'll send the plan Friday.")], fake)
        # Force the first batch out of the live window so the second submit
        # re-batches nothing it holds, then submit a second item.
        conn.execute(
            "UPDATE model_batch SET created_at = '2026-07-01T00:00:00+00:00'"
            " WHERE batch_id = 'msgbatch_001'"
        )
        _submit(conn, settings, boundary, [_mail(2, "Deciding on the vendor.")], fake)
        conn.execute(
            "UPDATE model_batch SET created_at = ? WHERE batch_id = 'msgbatch_001'",
            ("2026-08-01T00:00:00+00:00",),
        )
        fake.results_by_batch["msgbatch_001"] = [_succeeded("si-1", EXTRACTION)]
        fake.results_by_batch["msgbatch_002"] = [_succeeded("si-2", EXTRACTION)]

        report = batch_mod.collect(conn, settings, anthropic_client=fake)

        assert report.batches == 2
        per_batch = [
            int(r["spend_cents"])
            for r in conn.execute(
                "SELECT spend_cents FROM model_batch ORDER BY batch_id"
            )
        ]
        assert per_batch == [2, 2], "each batch bills itself, not its predecessors"
        assert sum(per_batch) == report.spend_cents

    def test_second_collect_is_a_no_op(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        fake = FakeBatches()
        _submit(conn, settings, boundary, [_mail(1, "I'll send the plan Friday.")], fake)
        fake.results_by_batch["msgbatch_001"] = [_succeeded("si-1", EXTRACTION)]
        batch_mod.collect(conn, settings, anthropic_client=fake)

        second = batch_mod.collect(conn, settings, anthropic_client=fake)
        assert second.batches == 0 and second.extracted == 0
        n = conn.execute("SELECT COUNT(*) AS n FROM commitment").fetchone()["n"]
        assert n == 1

    def test_interleaved_sync_stamp_means_already_done_no_duplicates(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        fake = FakeBatches()
        _submit(conn, settings, boundary, [_mail(1, "I'll send the plan Friday.")], fake)
        # A sync got there first (simulating the crash-recovery race): expire the
        # batch so sync sees the item, then extract synchronously.
        conn.execute("UPDATE model_batch SET status = 'expired'")
        model = FakeModel({"Plan 1": EXTRACTION})
        sync(conn, settings, [make_connector([], boundary)], model)
        conn.execute("UPDATE model_batch SET status = 'submitted'")

        fake.results_by_batch["msgbatch_001"] = [_succeeded("si-1", EXTRACTION)]
        report = batch_mod.collect(conn, settings, anthropic_client=fake)

        assert report.already_done == 1
        assert report.extracted == 0
        n = conn.execute("SELECT COUNT(*) AS n FROM commitment").fetchone()["n"]
        assert n == 1, "no duplicate commitments"

    def test_errored_items_stay_pending_and_next_sync_recovers_them(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        fake = FakeBatches()
        _submit(conn, settings, boundary, [_mail(1, "I'll send the plan Friday.")], fake)
        fake.results_by_batch["msgbatch_001"] = [_errored("si-1")]

        report = batch_mod.collect(conn, settings, anthropic_client=fake)
        assert report.failed_items == 1
        assert report.exit_code == 1
        stamped = conn.execute(
            "SELECT extraction_version FROM source_item WHERE id = 1"
        ).fetchone()["extraction_version"]
        assert stamped is None, "errored item must remain pending"

        model = FakeModel({"Plan 1": EXTRACTION})
        recovery = sync(conn, settings, [make_connector([], boundary)], model)
        assert recovery.extracted == 1, "degrade never block, end to end"

    def test_unfinished_recent_batch_waits_unfinished_stale_batch_expires(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        fake = FakeBatches()
        _submit(conn, settings, boundary, [_mail(1, "I'll send the plan Friday.")], fake)
        fake.status = "in_progress"

        waiting = batch_mod.collect(conn, settings, anthropic_client=fake)
        assert waiting.still_processing == 1
        assert conn.execute(
            "SELECT status FROM model_batch"
        ).fetchone()["status"] == "submitted"

        conn.execute(
            "UPDATE model_batch SET created_at = '2026-07-01T00:00:00+00:00'"
        )
        expired = batch_mod.collect(conn, settings, anthropic_client=fake)
        assert any("expired" in e for e in expired.errors)
        assert conn.execute(
            "SELECT status FROM model_batch"
        ).fetchone()["status"] == "expired"

    def test_out_of_order_results_apply_in_occurred_at_order(
        self, conn: sqlite3.Connection, settings: Settings, boundary
    ) -> None:
        """Supersession is application-order dependent; results arrive unordered."""
        promise = _mail(1, "I'll have the revised migration plan to you by Friday.")
        resolution = _mail(2, "Here is that migration plan I promised.")
        fake = FakeBatches()
        _submit(conn, settings, boundary, [promise, resolution], fake)

        resolves = {
            "commitments": [
                {
                    "direction": "owed_to_me",
                    "counterparty": "Dana Whitfield <dwhitfield@example.gov>",
                    "what": "revised migration plan",
                    "due_at": None,
                    "due_is_explicit": False,
                    "estimated_minutes": None,
                    "confidence": 0.9,
                    "evidence": "Here is that migration plan I promised.",
                    "resolves": True,
                    "resolves_what": "revised migration plan",
                }
            ]
        }
        # Delivered resolution-first — application must still be occurred_at order.
        fake.results_by_batch["msgbatch_001"] = [
            _succeeded("si-2", resolves),
            _succeeded("si-1", EXTRACTION),
        ]
        batch_mod.collect(conn, settings, anthropic_client=fake)

        rows = list(conn.execute("SELECT status FROM commitment ORDER BY id"))
        statuses = {r["status"] for r in rows}
        assert "superseded" in statuses or "done" in statuses, (
            f"the promise must be resolved by the later message; got {statuses}"
        )
