"""Chat commitments re-read against what the conversation said next.

84 open commitments came from `imessage` across 16 conversations; 79 had later messages in
the same chat and not one had ever closed from one. Closure only ever fired forward — a
message announcing it completed an earlier promise — and friends do not talk that way.

The negatives are the point of this file. A wrong open row is visible and costs a click; a
wrong close is silent data loss, and tasks/lessons.md has four entries about paying for
that. So every guard in `recheck.parse` gets a test that fails without it.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from backglass.config import Settings
from backglass.extract import prompts, recheck
from backglass.ledger import USER_ID

CHAT = "Pih ball"


@pytest.fixture
def prompt():  # type: ignore[no-untyped-def]
    return prompts.load("recheck-commitments")


def _chat(conn: sqlite3.Connection, key: str = CHAT) -> int:
    conn.execute(
        "INSERT INTO monitored_chat (user_id, source, key, display_name, kind, decision,"
        " messages_seen, first_seen_at, last_seen_at)"
        " VALUES (?, 'imessage', ?, ?, 'group', 'monitor', 0,"
        " '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')",
        (USER_ID, key.lower(), key),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _msg(conn: sqlite3.Connection, text: str, *, author: str = "Jisan", chat: str = CHAT) -> int:
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, content_hash, triage_verdict, extraction_version)"
        " VALUES (?, 'imessage', ?, '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z',"
        " ?, ?, ?, ?, 'keep', 'manual')",
        (USER_ID, f"m{text[:20]}{author}", author, chat, text, f"h{text[:24]}{author}"),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _commitment(conn: sqlite3.Connection, what: str, item_id: int) -> int:
    conn.execute(
        "INSERT INTO commitment (user_id, direction, what, confidence, status,"
        " estimated_minutes, estimate_source, source_item_id, created_at)"
        " VALUES (?, 'i_owe', ?, 0.9, 'open', 30, 'manual', ?, '2026-08-10T00:00:00Z')",
        (USER_ID, what, item_id),
    )
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _scene(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """A promise, then two later messages — one of which settles it."""
    chat_id = _chat(conn)
    promised = _msg(conn, "can you bring dress shoes tomorrow", author="Jisan")
    cid = _commitment(conn, "bring dress shoes", promised)
    settles = _msg(conn, "yeah I dropped the shoes off this morning", author="me")
    return chat_id, cid, settles


class FakeResult:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.cost_usd = 0.01


class FakeClient:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls = 0

    def complete(self, **_kwargs: Any) -> FakeResult:
        self.calls += 1
        return FakeResult(self.data)


class TestCandidates:
    def test_a_chat_with_nothing_new_produces_no_call(
        self, conn: sqlite3.Connection, settings: Settings, prompt: Any
    ) -> None:
        """Rule 3 in the form that matters on a 30-minute timer: the watermark means an
        unchanged conversation costs nothing at all, not merely zero writes."""
        chat_id, _, settles = _scene(conn)
        conn.execute(
            "UPDATE monitored_chat SET rechecked_through = ? WHERE id = ?",
            (settles, chat_id),
        )
        assert recheck.candidates(conn) == []
        client = FakeClient({"verdicts": []})
        recheck.run(conn, settings, client, prompt=prompt)
        assert client.calls == 0

    def test_a_chat_with_no_open_commitments_is_skipped(
        self, conn: sqlite3.Connection
    ) -> None:
        _chat(conn)
        _msg(conn, "hello")
        assert recheck.candidates(conn) == []

    def test_the_window_and_the_open_list_reach_the_prompt(
        self, conn: sqlite3.Connection, prompt: Any
    ) -> None:
        _, cid, settles = _scene(conn)
        [work] = recheck.candidates(conn)
        _, user = recheck.render(work, prompt=prompt)
        assert f"[{cid}] bring dress shoes" in user
        assert f"(msg {settles})" in user
        assert "dropped the shoes off" in user


class TestGuards:
    """Each of `parse`'s four discards. Every one of these, without its guard, is a
    silently closed obligation."""

    def _work(self, conn: sqlite3.Connection) -> recheck.ChatWork:
        [work] = recheck.candidates(conn)
        return work

    def test_a_cited_closure_is_kept(self, conn: sqlite3.Connection) -> None:
        _, cid, settles = _scene(conn)
        verdicts, discarded = recheck.parse(
            {"verdicts": [{
                "commitment_id": cid, "verdict": "done", "confidence": 0.95,
                "cites": settles, "quote": "dropped the shoes off this morning",
            }]},
            self._work(conn),
        )
        assert discarded == 0
        assert [v.verdict for v in verdicts] == ["done"]

    def test_a_closure_with_no_citation_is_discarded(self, conn: sqlite3.Connection) -> None:
        """Silence is not evidence. This is the verdict that would close every real
        obligation the owner has been quietly failing to do."""
        _, cid, _ = _scene(conn)
        verdicts, discarded = recheck.parse(
            {"verdicts": [{"commitment_id": cid, "verdict": "done", "confidence": 0.99}]},
            self._work(conn),
        )
        assert verdicts == [] and discarded == 1

    def test_a_foreign_commitment_id_is_discarded(self, conn: sqlite3.Connection) -> None:
        """Every id here is a bare int and the tables overlap in range, so a wrong-table
        id validates and closes a stranger (2026-08-12)."""
        _, _, settles = _scene(conn)
        verdicts, discarded = recheck.parse(
            {"verdicts": [{
                "commitment_id": 99999, "verdict": "done", "confidence": 0.99,
                "cites": settles, "quote": "dropped the shoes off this morning",
            }]},
            self._work(conn),
        )
        assert verdicts == [] and discarded == 1

    def test_a_citation_outside_the_window_is_discarded(
        self, conn: sqlite3.Connection
    ) -> None:
        _, cid, _ = _scene(conn)
        other = _msg(conn, "unrelated", chat="Family")
        verdicts, discarded = recheck.parse(
            {"verdicts": [{
                "commitment_id": cid, "verdict": "done", "confidence": 0.99,
                "cites": other, "quote": "unrelated",
            }]},
            self._work(conn),
        )
        assert verdicts == [] and discarded == 1

    def test_a_quote_that_is_not_in_the_message_is_discarded(
        self, conn: sqlite3.Connection
    ) -> None:
        """The guard against a fluent paraphrase standing in for evidence. Checking it
        costs one substring test; not checking it costs a commitment."""
        _, cid, settles = _scene(conn)
        verdicts, discarded = recheck.parse(
            {"verdicts": [{
                "commitment_id": cid, "verdict": "done", "confidence": 0.99,
                "cites": settles, "quote": "I already gave you the shoes back",
            }]},
            self._work(conn),
        )
        assert verdicts == [] and discarded == 1

    def test_an_open_verdict_needs_no_citation(self, conn: sqlite3.Connection) -> None:
        """Asymmetric on purpose: leaving something open asserts nothing."""
        _, cid, _ = _scene(conn)
        verdicts, discarded = recheck.parse(
            {"verdicts": [{"commitment_id": cid, "verdict": "open", "confidence": 0.4}]},
            self._work(conn),
        )
        assert discarded == 0 and [v.verdict for v in verdicts] == ["open"]


class TestApply:
    def _run(self, conn: sqlite3.Connection, settings: Settings, prompt: Any,
             data: dict[str, Any], **kw: Any) -> recheck.Report:
        return recheck.run(conn, settings, FakeClient(data), prompt=prompt, **kw)

    def test_a_confident_verdict_closes_the_commitment_with_its_citation(
        self, conn: sqlite3.Connection, settings: Settings, prompt: Any
    ) -> None:
        _, cid, settles = _scene(conn)
        report = self._run(conn, settings, prompt, {"verdicts": [{
            "commitment_id": cid, "verdict": "done", "confidence": 0.95,
            "cites": settles, "quote": "dropped the shoes off this morning",
        }]})
        assert report.applied == 1
        row = conn.execute(
            "SELECT status, resolution_note FROM commitment WHERE id = ?", (cid,)
        ).fetchone()
        assert row["status"] == "done"
        # CLAUDE.md rule 1: the close carries the words that justified it.
        assert "dropped the shoes off" in str(row["resolution_note"])
        assert f"msg {settles}" in str(row["resolution_note"])

    def test_a_low_confidence_verdict_waits_instead_of_closing(
        self, conn: sqlite3.Connection, settings: Settings, prompt: Any
    ) -> None:
        """A wrong open row is visible and dismissible; a wrong close is silent."""
        _, cid, settles = _scene(conn)
        report = self._run(conn, settings, prompt, {"verdicts": [{
            "commitment_id": cid, "verdict": "done", "confidence": 0.2,
            "cites": settles, "quote": "dropped the shoes off this morning",
        }]})
        assert (report.applied, report.pending) == (0, 1)
        assert conn.execute(
            "SELECT status FROM commitment WHERE id = ?", (cid,)
        ).fetchone()["status"] == "open"
        assert conn.execute(
            "SELECT status FROM commitment_recheck WHERE commitment_id = ?", (cid,)
        ).fetchone()["status"] == "pending"

    def test_an_already_closed_commitment_is_left_alone(
        self, conn: sqlite3.Connection, settings: Settings, prompt: Any
    ) -> None:
        """Re-read at write time: the owner may have closed it by hand while the model
        was thinking."""
        _, cid, settles = _scene(conn)
        conn.execute("UPDATE commitment SET status = 'dropped' WHERE id = ?", (cid,))
        report = self._run(conn, settings, prompt, {"verdicts": [{
            "commitment_id": cid, "verdict": "done", "confidence": 0.99,
            "cites": settles, "quote": "dropped the shoes off this morning",
        }]})
        assert report.applied == 0
        assert conn.execute(
            "SELECT status FROM commitment WHERE id = ?", (cid,)
        ).fetchone()["status"] == "dropped"

    def test_a_second_run_writes_nothing(
        self, conn: sqlite3.Connection, settings: Settings, prompt: Any
    ) -> None:
        """Rule 3. The watermark makes the second pass make no call at all."""
        _, cid, settles = _scene(conn)
        data = {"verdicts": [{
            "commitment_id": cid, "verdict": "done", "confidence": 0.95,
            "cites": settles, "quote": "dropped the shoes off this morning",
        }]}
        self._run(conn, settings, prompt, data)
        before = conn.execute("SELECT COUNT(*) AS n FROM commitment_recheck").fetchone()["n"]
        client = FakeClient(data)
        recheck.run(conn, settings, client, prompt=prompt)
        after = conn.execute("SELECT COUNT(*) AS n FROM commitment_recheck").fetchone()["n"]
        assert client.calls == 0
        assert before == after == 1

    def test_dry_run_writes_nothing_at_all(
        self, conn: sqlite3.Connection, settings: Settings, prompt: Any
    ) -> None:
        _, cid, settles = _scene(conn)
        report = self._run(conn, settings, prompt, {"verdicts": [{
            "commitment_id": cid, "verdict": "done", "confidence": 0.95,
            "cites": settles, "quote": "dropped the shoes off this morning",
        }]}, dry_run=True)
        assert report.applied == 1
        assert conn.execute(
            "SELECT status FROM commitment WHERE id = ?", (cid,)
        ).fetchone()["status"] == "open"
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM commitment_recheck"
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT rechecked_through FROM monitored_chat"
        ).fetchone()["rechecked_through"] is None

    def test_one_dead_conversation_does_not_stop_the_others(
        self, conn: sqlite3.Connection, settings: Settings, prompt: Any
    ) -> None:
        """Rule 5's unit is the loop item."""
        _scene(conn)
        _chat(conn, "Family")
        promised = _msg(conn, "pick up the parcel", author="Amma", chat="Family")
        _commitment(conn, "pick up the parcel", promised)
        _msg(conn, "got it thanks", author="Amma", chat="Family")

        class Flaky(FakeClient):
            def complete(self, **kwargs: Any) -> FakeResult:
                self.calls += 1
                if self.calls == 1:
                    raise OSError("connection reset")
                return FakeResult({"verdicts": []})

        report = recheck.run(conn, settings, Flaky({}), prompt=prompt)
        assert len(report.checked) == 2
        assert len(report.errors) == 1


class TestTheModelIsNeverCalledLive:
    def test_the_prompt_declares_its_version(self, prompt: Any) -> None:
        assert prompt.stamp == "recheck-commitments@1"

    def test_both_placeholders_sit_in_the_cacheable_tail(self, prompt: Any) -> None:
        """`Prompt.split`'s ruling: a rule written above a placeholder is a rule paid for
        on every call forever. This prompt was written to obey it."""
        static, dynamic = prompt.split()
        assert "{{" not in static
        assert "{{commitments}}" in dynamic and "{{messages}}" in dynamic
        assert len(static) > 1000


class TestTheReviewQueue:
    """Step 7: a verdict under the threshold is a question, and one click answers it."""

    def _pending(self, conn: sqlite3.Connection, settings: Settings, prompt: Any) -> int:
        _, cid, settles = _scene(conn)
        recheck.run(conn, settings, FakeClient({"verdicts": [{
            "commitment_id": cid, "verdict": "done", "confidence": 0.2,
            "cites": settles, "quote": "dropped the shoes off this morning",
        }]}), prompt=prompt)
        return int(conn.execute(
            "SELECT id FROM commitment_recheck WHERE status = 'pending'"
        ).fetchone()["id"])

    def test_it_appears_in_the_queue_with_its_evidence(
        self, conn: sqlite3.Connection, settings: Settings, prompt: Any
    ) -> None:
        from backglass.web import panels

        self._pending(conn, settings, prompt)
        rows = [r for r in panels.review_panel(conn, settings).rows if r["record"] == "recheck"]
        assert len(rows) == 1
        # The owner is agreeing with evidence, not trusting the machine.
        assert rows[0]["quote"] == "dropped the shoes off this morning"
        assert rows[0]["what"] == "bring dress shoes"

    def test_confirming_closes_it_and_keeps_the_citation(
        self, conn: sqlite3.Connection, settings: Settings, prompt: Any
    ) -> None:
        from backglass.web import actions

        rid = self._pending(conn, settings, prompt)
        actions.confirm_recheck(conn, rid)
        row = conn.execute(
            "SELECT c.status, c.resolution_note, r.status AS rstatus"
            "  FROM commitment_recheck r JOIN commitment c ON c.id = r.commitment_id"
            " WHERE r.id = ?", (rid,)
        ).fetchone()
        assert row["status"] == "done" and row["rstatus"] == "applied"
        assert "dropped the shoes off" in str(row["resolution_note"])

    def test_dismissing_keeps_it_open_and_is_remembered(
        self, conn: sqlite3.Connection, settings: Settings, prompt: Any
    ) -> None:
        """An unremembered "no" re-surfaces every morning forever — the same reasoning
        `commitment_distinct` exists for."""
        from backglass.web import actions

        rid = self._pending(conn, settings, prompt)
        actions.dismiss_recheck(conn, rid)
        assert conn.execute(
            "SELECT status FROM commitment_recheck WHERE id = ?", (rid,)
        ).fetchone()["status"] == "dismissed"
        assert not [
            r for r in __import__("backglass.web.panels", fromlist=["x"])
            .review_panel(conn, settings).rows if r["record"] == "recheck"
        ]

    def test_a_decided_verdict_cannot_be_decided_twice(
        self, conn: sqlite3.Connection, settings: Settings, prompt: Any
    ) -> None:
        from backglass.web import actions

        rid = self._pending(conn, settings, prompt)
        actions.confirm_recheck(conn, rid)
        with pytest.raises(actions.ActionError):
            actions.confirm_recheck(conn, rid)
