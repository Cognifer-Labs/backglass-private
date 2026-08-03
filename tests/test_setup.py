"""Phase A2: connector auto-detection, the .env writer, and `backglass setup`.

Detection is probed against a fake home tree built in tmp_path — the real home
directory is never read (docs/10 §Testing in spirit: no test depends on what
happens to be installed on the machine running it). The CLI test monkeypatches
`get_settings` and the home probe so a run from the repo checkout can never touch
the real .env or the real database.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

import backglass.__main__ as cli
from backglass import envfile
from backglass.config import Settings
from backglass.connectors import detect

# ─────────────────────────────────────────────────────────────── fake home


def _store(path: Path) -> Path:
    """A real, openable SQLite file.

    Detection opens local stores rather than stat-ing them, so a placeholder byte
    would now read as an unreadable store — and a fixture that cannot be opened is
    not standing in for a collection anyone could sync from anyway.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")
    return path


def _fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    old = _store(home / "Library/Application Support/Anki2/Old profile/collection.anki2")
    _store(home / "Library/Application Support/Anki2/User 1/collection.anki2")
    stale = time.time() - 10_000
    os.utime(old, (stale, stale))

    _store(home / "Library/Application Support/Avorio/avorio.db")

    obsidian = home / "Library/Application Support/obsidian"
    obsidian.mkdir(parents=True)
    vault = home / "Vaults/Second Brain"
    vault.mkdir(parents=True)
    obsidian.joinpath("obsidian.json").write_text(
        json.dumps({"vaults": {"a1": {"path": str(vault)}}})
    )
    return home


def _by_source(rows: list[detect.Detection]) -> dict[str, detect.Detection]:
    return {d.source: d for d in rows}


@pytest.fixture
def bare(settings: Settings) -> Settings:
    """The conftest Settings still inherits the owner's live .env for fields it
    does not pin — detection must be probed from a machine-state of nothing."""
    return settings.model_copy(
        update={
            "anki_db_path": None,
            "avorio_db_path": None,
            "imessage_db_path": None,
            "obsidian_vault_path": None,
            "inbox_folder_path": None,
            "apple_notes": False,
            "apple_reminders": False,
            "github_token": "",
            "slack_token": "",
            "slack_channels": [],
            "canvas_base_url": "",
            "canvas_token": "",
            "google_client_id": "",
            "google_client_secret": "",
            "gmail_accounts": [],
            "calendar_accounts": [],
            "drive_accounts": [],
            "reviews_target_id": None,
        }
    )


class TestDetect:
    def test_local_stores_are_found_with_ready_env_lines(
        self, bare: Settings, tmp_path: Path
    ) -> None:
        home = _fake_home(tmp_path)
        d = _by_source(detect.detect_all(bare, home=home))
        assert d["anki"].status == detect.FOUND
        expected = home / "Library/Application Support/Anki2/User 1/collection.anki2"
        assert d["anki"].env_line == f"ANKI_DB_PATH={expected}"
        assert "User 1" in d["anki"].hint  # newest profile wins
        assert d["avorio"].status == detect.FOUND
        assert d["notes"].status == detect.FOUND
        assert d["notes"].env_key == "OBSIDIAN_VAULT_PATH"

    def test_invisible_chat_db_reads_as_permission_not_missing(
        self, bare: Settings, tmp_path: Path
    ) -> None:
        d = _by_source(detect.detect_all(bare, home=_fake_home(tmp_path)))
        assert d["imessage"].status == detect.NEEDS_SETUP
        assert "Full Disk Access" in d["imessage"].hint

    def test_present_but_unopenable_chat_db_is_not_configured(
        self, bare: Settings, tmp_path: Path
    ) -> None:
        """The defect this probe exists for.

        Full Disk Access is granted per-binary, so on a machine without it chat.db
        stats fine and refuses to open — detection used to return `configured`
        straight off the .env path and every sync failed behind a green Sources
        panel. A file that is not a database stands in for the refused open: the
        branch under test is "the store did not open", not any one errno.
        """
        home = _fake_home(tmp_path)
        chat = home / "Library/Messages/chat.db"
        chat.parent.mkdir(parents=True)
        chat.write_bytes(b"not a database")
        bound = bare.model_copy(update={"imessage_db_path": chat})

        d = _by_source(detect.detect_all(bound, home=home))

        assert d["imessage"].status == detect.NEEDS_SETUP
        assert "Full Disk Access" in d["imessage"].hint

    def test_readable_chat_db_is_configured(
        self, bare: Settings, tmp_path: Path
    ) -> None:
        """The pass branch, so the probe is known to go green as well as red."""
        home = _fake_home(tmp_path)
        chat = _store(home / "Library/Messages/chat.db")
        bound = bare.model_copy(update={"imessage_db_path": chat})
        d = _by_source(detect.detect_all(bound, home=home))
        assert d["imessage"].status == detect.CONFIGURED

    def test_configured_sources_report_configured(
        self, bare: Settings, tmp_path: Path
    ) -> None:
        home = _fake_home(tmp_path)
        bound = bare.model_copy(
            update={"anki_db_path": _store(home / "x.anki2"), "github_token": "ghp_x"}
        )
        d = _by_source(detect.detect_all(bound, home=home))
        assert d["anki"].status == detect.CONFIGURED
        assert d["github"].status == detect.CONFIGURED

    def test_configured_store_that_vanished_stops_reading_configured(
        self, bare: Settings, tmp_path: Path
    ) -> None:
        """An .env path is a claim about the past. Anki uninstalled, or the profile
        renamed, must not leave the source green on the Sources panel."""
        home = _fake_home(tmp_path)
        bound = bare.model_copy(update={"anki_db_path": home / "gone.anki2"})
        d = _by_source(detect.detect_all(bound, home=home))
        assert d["anki"].status == detect.MISSING

    def test_drifted_obsidian_registry_degrades_never_raises(
        self, bare: Settings, tmp_path: Path
    ) -> None:
        """Verifier caveat: detection runs on every dashboard render, so a
        registry entry of an unexpected shape must read as missing, not 500."""
        home = tmp_path / "home-drift"
        obsidian = home / "Library/Application Support/obsidian"
        obsidian.mkdir(parents=True)
        obsidian.joinpath("obsidian.json").write_text('{"vaults": {"a": "/not/a/dict"}}')
        d = _by_source(detect.detect_all(bare, home=home))
        assert d["notes"].status == detect.MISSING

    def test_empty_machine_reports_missing_not_found(
        self, bare: Settings, tmp_path: Path
    ) -> None:
        home = tmp_path / "empty-home"
        home.mkdir()
        d = _by_source(detect.detect_all(bare, home=home))
        assert d["anki"].status == detect.MISSING
        assert d["avorio"].status == detect.MISSING
        assert d["notes"].status == detect.MISSING

    def test_authed_google_account_is_configured(
        self, bare: Settings, tmp_path: Path
    ) -> None:
        bound = bare.model_copy(
            update={
                "google_client_id": "cid",
                "google_client_secret": "sec",
                "gmail_accounts": ["personal"],
            }
        )
        home = tmp_path / "empty-home2"
        home.mkdir()
        pending = _by_source(detect.detect_all(bound, home=home))
        assert pending["gmail:personal"].status == detect.NEEDS_SETUP
        assert "backglass auth personal" in pending["gmail:personal"].hint
        authed = _by_source(detect.detect_all(bound, home=home, authed={"gmail:personal"}))
        assert authed["gmail:personal"].status == detect.CONFIGURED


# ───────────────────────────────────────────────────────────── env writer


class TestEnvfile:
    def test_replaces_in_place_and_preserves_everything_else(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("# a comment\nANKI_DB_PATH=\nOWNER_EMAILS=a@b.c\n")
        changed = envfile.set_keys(env, {"ANKI_DB_PATH": "/tmp/c.anki2"})
        assert changed == ["ANKI_DB_PATH"]
        assert env.read_text() == "# a comment\nANKI_DB_PATH=/tmp/c.anki2\nOWNER_EMAILS=a@b.c\n"

    def test_appends_missing_keys_under_one_heading(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("OWNER_EMAILS=a@b.c\n")
        envfile.set_keys(env, {"AVORIO_DB_PATH": "/tmp/a.db"})
        envfile.set_keys(env, {"REVIEWS_TARGET_ID": "4"})
        text = env.read_text()
        assert text.count("added by `backglass setup`") == 1
        assert "AVORIO_DB_PATH=/tmp/a.db" in text
        assert "REVIEWS_TARGET_ID=4" in text

    def test_idempotent_write_reports_nothing(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("ANKI_DB_PATH=/tmp/c.anki2\n")
        assert envfile.set_keys(env, {"ANKI_DB_PATH": "/tmp/c.anki2"}) == []

    def test_seeds_from_template_when_absent(self, tmp_path: Path) -> None:
        template = tmp_path / ".env.example"
        template.write_text("# documented\nANKI_DB_PATH=\n")
        env = tmp_path / ".env"
        envfile.set_keys(env, {"ANKI_DB_PATH": "/tmp/c"}, template=template)
        assert env.read_text() == "# documented\nANKI_DB_PATH=/tmp/c\n"

    def test_duplicate_keys_are_all_rewritten(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("K_EY=old\nK_EY=older\n")
        envfile.set_keys(env, {"K_EY": "new"})
        assert env.read_text() == "K_EY=new\nK_EY=new\n"


# ─────────────────────────────────────────────────────────── the command


@pytest.fixture
def isolated_cli(  # type: ignore[no-untyped-def]
    bare: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`backglass setup` wired to a fake home, a tmp db, and a tmp .env — a run
    from the repo checkout must never be able to reach the real ones."""
    home = _fake_home(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: bare)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


class TestSetupCommand:
    def test_yes_writes_every_found_source(
        self, isolated_cli: Path, tmp_path: Path
    ) -> None:
        env = tmp_path / ".env"
        result = CliRunner().invoke(
            cli.app, ["setup", "--yes", "--env-path", str(env)]
        )
        assert result.exit_code == 0, result.output
        text = env.read_text()
        assert "ANKI_DB_PATH=" in text and "User 1" in text
        assert "AVORIO_DB_PATH=" in text
        assert "OBSIDIAN_VAULT_PATH=" in text
        assert "backglass doctor" in result.output

    def test_rerun_after_configuration_is_quiet(
        self, isolated_cli: Path, bare: Settings, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        configured = bare.model_copy(
            update={
                "anki_db_path": tmp_path / "c.anki2",
                "avorio_db_path": tmp_path / "a.db",
                "obsidian_vault_path": tmp_path / "v",
                "apple_notes": True,
                "apple_reminders": True,
                "imessage_db_path": tmp_path / "chat.db",
            }
        )
        monkeypatch.setattr(cli, "get_settings", lambda: configured)
        env = tmp_path / ".env"
        result = CliRunner().invoke(cli.app, ["setup", "--yes", "--env-path", str(env)])
        assert result.exit_code == 0, result.output
        assert "nothing to write" in result.output
        assert not env.exists()

    def test_reviews_target_binding_validates_the_id(
        self, isolated_cli: Path, conn: sqlite3.Connection, tmp_path: Path,
    ) -> None:
        result = CliRunner().invoke(
            cli.app,
            ["setup", "--yes", "--env-path", str(tmp_path / ".env"),
             "--reviews-target", "999"],
        )
        assert result.exit_code == 1
        assert "no active cadence target 999" in result.output

    def test_reviews_target_binding_writes_the_id(
        self, isolated_cli: Path, conn: sqlite3.Connection, tmp_path: Path,
    ) -> None:
        conn.execute(
            "INSERT INTO goal (user_id, title, horizon, definition_of_done, created_at) "
            "VALUES (1, 'Med school', 'annual', 'Matched', '2026-01-01T00:00:00')"
        )
        goal_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        conn.execute(
            "INSERT INTO target (goal_id, kind, title, weekly_count, created_at) "
            "VALUES (?, 'cadence', 'Reviews done', 7, '2026-01-01T00:00:00')",
            (goal_id,),
        )
        tid = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        conn.commit()
        env = tmp_path / ".env"
        result = CliRunner().invoke(
            cli.app,
            ["setup", "--yes", "--env-path", str(env), "--reviews-target", str(tid)],
        )
        assert result.exit_code == 0, result.output
        assert f"REVIEWS_TARGET_ID={tid}" in env.read_text()


# ───────────────────────────────────────────────────────── panel surfacing


class TestPanelSurfacing:
    def test_found_stores_appear_in_the_sources_panel_meta(
        self, conn: sqlite3.Connection, bare: Settings, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from backglass.web import panels

        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        panel = panels.sources_panel(conn, bare)
        names = {f["source"] for f in panel.meta["found"]}
        assert {"anki", "avorio", "notes"} <= names
        assert panel.empty_text == "No sources configured. Run `backglass setup`."
