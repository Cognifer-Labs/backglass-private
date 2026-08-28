"""What a hand-run pass spends, in the table everything reads spend from.

CLAUDE.md rule 7: the spend cap is enforced in code. `sync` has always written its calls
to `model_call`; the three passes that can also be run by hand — `recheck`, `relevance`,
`revise` — collected a `CallRecord` per call into a local list and then dropped it on the
floor. So a pass run from the terminal spent money that `state`'s per-tier cost, the
Sources panel and the cap itself could not see.

Invisible on the free backend, which is what let it sit: every row reads 0.0 either way.
Found on 2026-08-27 by running `revise` on claude_cli, watching the CLI report 205c, and
finding no row for it anywhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from typer.testing import CliRunner

import backglass.__main__ as cli
from backglass.config import Settings
from backglass.db import connect, migrate
from backglass.telemetry import CallRecord, write_calls


def test_write_calls_takes_a_pass_with_no_run_behind_it(tmp_path: Path) -> None:
    """`run_id` is NULL for a hand-run pass. The column has always allowed it; nothing
    had ever passed None, so this is the assertion that the door opens."""
    conn = connect(tmp_path / "b.db")
    migrate(conn)
    written = write_calls(
        conn,
        [
            CallRecord(
                tier="revise", model="claude-opus", prompt_chars=10, duration_ms=5,
                cost_usd=2.05, outcome="ok", started_at="2026-08-27T19:00:00+00:00",
            )
        ],
        None,
    )
    conn.commit()
    assert written == 1
    row = conn.execute(
        "SELECT tier, run_id, cost_usd FROM model_call"
    ).fetchone()
    assert (row["tier"], row["run_id"], row["cost_usd"]) == ("revise", None, 2.05)


class _Recorder:
    """A client that charges for one call, so there is something to lose."""

    spend_is_imputed = False

    def complete(self, **_: Any) -> Any:
        return type("R", (), {"data": {"verdicts": []}, "cost_usd": 1.5})()


def _seed(settings: Settings) -> None:
    from backglass.db import now_iso
    from backglass.ledger import USER_ID

    conn = connect(settings.db_path)
    migrate(conn)
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at,"
        " author, title, body_text, raw_json, content_hash, triage_verdict)"
        " VALUES (?, 'apple-mail', 'x', ?, '2026-08-20T00:00:00+00:00', 'A', 't', 'b',"
        " NULL, 'h', 'keep')",
        (USER_ID, now_iso()),
    )
    conn.execute(
        "INSERT INTO fact (user_id, subject, key, value, source, status, created_at)"
        " VALUES (?, 'work', 'k', 'v', 'manual', 'active', ?)",
        (USER_ID, now_iso()),
    )
    conn.commit()
    conn.close()


def test_a_hand_run_pass_records_what_it_spent(
    monkeypatch: Any, settings: Settings
) -> None:
    """The whole point: run the command, then look in the table the cap reads."""
    _seed(settings)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_build_model_client", lambda _s: _Recorder())

    result = CliRunner().invoke(cli.app, ["revise"])
    assert result.exit_code == 0, result.output

    conn = connect(settings.db_path)
    rows = conn.execute(
        "SELECT tier, run_id, cost_usd FROM model_call ORDER BY id"
    ).fetchall()
    assert [r["tier"] for r in rows] == ["revise"]
    assert rows[0]["run_id"] is None
    assert rows[0]["cost_usd"] == 1.5
