"""`backglass brief --send` when email delivery is not (fully) configured.

Never-configured and misconfigured are different states and get different exits.
BRIEF_TO empty means the owner reads the brief in the app: the brief persists, the
command says so, and the 06:00 launchd job stops logging the same non-error every
morning forever. BRIEF_TO set with the rest missing means someone asked for mail
that cannot arrive — that stays a failure.
"""

from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from backglass import __main__ as cli
from backglass.config import Settings


def _briefs(settings: Settings) -> list[sqlite3.Row]:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT id, sent_at FROM brief"))
    finally:
        conn.close()


def test_send_with_no_recipient_is_a_clean_skip(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    result = CliRunner().invoke(cli.app, ["brief", "--send"])

    assert result.exit_code == 0, result.output
    assert "BRIEF_TO is not set" in result.output
    (row,) = _briefs(settings)  # persisted and readable in-app, just not mailed
    assert row["sent_at"] is None


def test_send_with_a_recipient_but_no_key_still_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings.model_copy(update={"brief_to": "owner@example.com"})
    monkeypatch.setattr(cli, "get_settings", lambda: configured)

    result = CliRunner().invoke(cli.app, ["brief", "--send"])

    assert result.exit_code == 1
    assert "RESEND_API_KEY" in result.output
