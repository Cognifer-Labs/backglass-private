"""What this machine hands to everything else running on it.

Three separate findings, one property: the owner's secrets and their ledger are for the
owner. docs/08 §General handling — "Tokens never appear in logs, in the SQLite file
outside the `credential` table, or in brief output" — is about the token never leaking
sideways, and a 0644 file holding the `credential` table is a sideways leak that needs no
bug to exploit, just a second account or a curious process on the same Mac.

  - the SQLite file and its WAL sidecars carry the Google access and refresh tokens
  - the .env carries six third-party API keys
  - the dashboard reads *and writes* the ledger with no login at all

All three were created under the default umask, or bindable to any interface, until this
file existed. These are the regressions, not a style check: each assertion is the exact
exposure the audit found.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

import backglass.__main__ as cli
from backglass import envfile
from backglass.config import Settings
from backglass.db import connect


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ──────────────────────────────────────────────────────────── the database


class TestDatabaseMode:
    def test_a_new_database_is_owner_only(self, tmp_path: Path) -> None:
        db = tmp_path / "nested" / "a.db"
        conn = connect(db)
        assert mode(db) == 0o600
        conn.close()

    def test_the_wal_sidecars_are_owner_only_too(self, tmp_path: Path) -> None:
        """The -wal holds committed pages not yet checkpointed back, so a readable
        sidecar leaks the same `credential` rows the database does.

        They appear on first write, not at connect. SQLite gives them the mode of the
        database file it opened, which is why restricting the main file *before* handing
        it to sqlite3.connect is what actually covers all three.
        """
        db = tmp_path / "b.db"
        conn = connect(db)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        sidecars = [db.with_name(db.name + suffix) for suffix in ("-wal", "-shm")]
        assert all(p.exists() for p in sidecars), "a write should have made both sidecars"
        for path in sidecars:
            assert mode(path) == 0o600, path.name
        conn.close()

    def test_sidecars_left_open_and_readable_are_tightened_on_the_next_connect(
        self, tmp_path: Path
    ) -> None:
        """The dashboard reads while the sync writes, so a second connection routinely
        arrives while sidecars from a pre-fix run are still on disk at 0644."""
        db = tmp_path / "e.db"
        writer = connect(db)
        writer.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        sidecars = [db.with_name(db.name + suffix) for suffix in ("-wal", "-shm")]
        for path in (db, *sidecars):
            os.chmod(path, 0o644)

        reader = connect(db)
        for path in (db, *sidecars):
            assert mode(path) == 0o600, path.name
        reader.close()
        writer.close()

    def test_an_existing_world_readable_database_is_tightened(self, tmp_path: Path) -> None:
        """The database that is at 0644 right now is the one with live tokens in it.
        Connecting to it has to fix the mode, not decline because it is pre-existing."""
        db = tmp_path / "c.db"
        connect(db).close()
        os.chmod(db, 0o644)
        conn = connect(db)
        assert mode(db) == 0o600
        conn.close()

    def test_an_unchmoddable_database_degrades_and_says_so(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """CLAUDE.md rule 5: a filesystem that cannot hold the mode is a degrade, not a
        crash — but silence would be indistinguishable from the mode being applied."""
        import backglass.db as db_mod

        monkeypatch.setattr(db_mod, "_mode_warned", set())

        def refuse(self: Path, mode_bits: int) -> None:
            raise PermissionError("read-only file system")

        db = tmp_path / "d.db"
        connect(db).close()
        os.chmod(db, 0o644)
        monkeypatch.setattr(Path, "chmod", refuse)

        conn = connect(db)
        assert conn.execute("SELECT 1 AS n").fetchone()["n"] == 1
        conn.close()
        assert "cannot restrict" in capsys.readouterr().err


# ──────────────────────────────────────────────────────────────── the .env


class TestEnvFileMode:
    def test_a_new_env_is_owner_only(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        envfile.set_keys(env, {"MODEL_API_KEY": "sk-secret"})
        assert mode(env) == 0o600

    def test_an_existing_world_readable_env_is_tightened(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("GITHUB_TOKEN=ghp_old\n")
        os.chmod(env, 0o644)
        envfile.set_keys(env, {"GITHUB_TOKEN": "ghp_new"})
        assert mode(env) == 0o600

    def test_the_seeded_file_is_private_before_the_template_lands(
        self, tmp_path: Path
    ) -> None:
        """The seed path creates the file; it must not be created wide and narrowed
        afterwards, because the very next write puts a key in it."""
        template = tmp_path / ".env.example"
        template.write_text("SLACK_TOKEN=\n")
        env = tmp_path / ".env"
        envfile.set_keys(env, {"SLACK_TOKEN": "xoxb-secret"}, template=template)
        assert mode(env) == 0o600
        assert env.read_text() == "SLACK_TOKEN=xoxb-secret\n"


class TestEnvValueInjection:
    """connectors/detect.py globs over ~/Library and ~/Downloads. A directory named with
    an embedded newline turns one `KEY=value` line into two, and the second one is an
    arbitrary setting — BOUNDARY_MODE among them, which is the docs/08 control itself."""

    @pytest.mark.parametrize(
        "value",
        [
            "/Users/k/notes\nBOUNDARY_MODE=off",
            "/Users/k/notes\r\nMODEL_BACKEND=none",
            "/Users/k/notes\rOWNER_EMAILS=attacker@example.com",
        ],
    )
    def test_a_line_break_in_a_value_is_refused(self, tmp_path: Path, value: str) -> None:
        env = tmp_path / ".env"
        env.write_text("BOUNDARY_MODE=deny\n")
        with pytest.raises(envfile.EnvValueError, match="line break"):
            envfile.set_keys(env, {"OBSIDIAN_VAULT_PATH": value})
        assert env.read_text() == "BOUNDARY_MODE=deny\n"

    def test_the_refusal_happens_before_any_write(self, tmp_path: Path) -> None:
        """Validated up front, so a bad value in a batch cannot leave the good keys
        half-applied — and cannot create the file at all when there wasn't one."""
        env = tmp_path / ".env"
        with pytest.raises(envfile.EnvValueError):
            envfile.set_keys(env, {"GITHUB_TOKEN": "ghp_ok", "CANVAS_TOKEN": "a\nb"})
        assert not env.exists()

    def test_setup_reports_it_instead_of_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> None:
        from backglass.connectors import detect as detect_mod

        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        # Detection is the real source of these values, so the hostile name is injected
        # there and the refusal travels the actual path — rather than stubbing set_keys,
        # which would only assert that `setup` re-raises what the test itself threw.
        monkeypatch.setattr(
            detect_mod,
            "detect_all",
            lambda *a, **k: [
                detect_mod.Detection(
                    source="obsidian",
                    status=detect_mod.FOUND,
                    env_key="OBSIDIAN_VAULT_PATH",
                    env_value="/Users/k/notes\nBOUNDARY_MODE=off",
                    hint="a vault whose directory name carries a line break",
                )
            ],
        )
        monkeypatch.setattr(Path, "home", classmethod(lambda c: tmp_path / "home"))
        result = CliRunner().invoke(
            cli.app, ["setup", "--yes", "--env-path", str(tmp_path / ".env")]
        )
        assert result.exit_code == 1
        assert "line break" in result.output


# ─────────────────────────────────────────────────────────── the dashboard


class TestDashboardBind:
    @pytest.fixture
    def bound(
        self, monkeypatch: pytest.MonkeyPatch, settings: Settings
    ) -> list[dict[str, object]]:
        import backglass.web.app as web

        calls: list[dict[str, object]] = []
        monkeypatch.setattr(cli, "get_settings", lambda: settings)
        monkeypatch.setattr(
            web, "serve", lambda s, *, host, port: calls.append({"host": host, "port": port})
        )
        return calls

    def test_loopback_is_unchanged_and_frictionless(
        self, bound: list[dict[str, object]]
    ) -> None:
        result = CliRunner().invoke(cli.app, ["dashboard"])
        assert result.exit_code == 0, result.output
        assert bound == [{"host": "127.0.0.1", "port": 8765}]

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.2", "::1"])
    def test_other_loopback_spellings_still_serve(
        self, bound: list[dict[str, object]], host: str
    ) -> None:
        result = CliRunner().invoke(cli.app, ["dashboard", "--host", host])
        assert result.exit_code == 0, result.output
        assert bound == [{"host": host, "port": 8765}]

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.14", "::", "example.local"])
    def test_a_non_loopback_bind_is_refused(
        self, bound: list[dict[str, object]], host: str
    ) -> None:
        """`--host 0.0.0.0` publishes an unauthenticated read/write view of the ledger to
        whatever network this Mac is on. One character from the default; not an accident
        the owner gets to make silently."""
        result = CliRunner().invoke(cli.app, ["dashboard", "--host", host])
        assert result.exit_code == 1
        assert "no login" in result.output
        assert bound == [], "nothing may bind before the guard"

    def test_the_opt_in_flag_names_the_risk_and_lets_it_through(
        self, bound: list[dict[str, object]]
    ) -> None:
        result = CliRunner().invoke(
            cli.app, ["dashboard", "--host", "0.0.0.0", "--expose-unauthenticated"]
        )
        assert result.exit_code == 0, result.output
        assert bound == [{"host": "0.0.0.0", "port": 8765}]
        assert "unauthenticated" in result.output

    def test_the_help_says_what_the_flag_costs(self) -> None:
        result = CliRunner().invoke(cli.app, ["dashboard", "--help"])
        assert "no login" in result.output.replace("\n", " ")
