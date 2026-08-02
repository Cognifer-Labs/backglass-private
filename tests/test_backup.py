"""Audit #23: snapshots of the ledger, and the way back from one.

Nothing here touches `data/backglass.db` — every test builds its own database in
`tmp_path` through the real `migrate()`, so a snapshot is taken of a realistically
shaped file (13 tables, WAL sidecars and all) rather than an empty one.
"""

from __future__ import annotations

import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backglass import __main__ as cli
from backglass import backup
from backglass.config import Settings
from backglass.db import connect, migrate, now_iso


@pytest.fixture
def live_db(tmp_path: Path) -> Path:
    """A real migrated database with a row in it, left open-able but not open."""
    path = tmp_path / "data" / "backglass.db"
    conn = connect(path)
    migrate(conn)
    _add_credential(conn, "gmail:test")
    conn.close()
    return path


def _add_credential(conn: sqlite3.Connection, source: str) -> None:
    conn.execute(
        "INSERT INTO credential (user_id, source, status, updated_at)"
        " VALUES (1, ?, 'ok', ?)",
        (source, now_iso()),
    )


def _aged(directory: Path, stamp: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"backglass-{stamp}.db"
    path.write_bytes(b"")
    return path


# ── snapshot ──────────────────────────────────────────────────────────────


class TestSnapshot:
    def test_writes_a_verifiable_copy_of_the_ledger(
        self, live_db: Path, tmp_path: Path
    ) -> None:
        out = backup.snapshot(live_db, tmp_path / "backups")

        assert out.parent == tmp_path / "backups"
        assert backup.verify(out)
        conn = sqlite3.connect(out)
        rows = conn.execute("SELECT source FROM credential").fetchall()
        conn.close()
        assert rows == [("gmail:test",)]

    def test_the_snapshot_is_owner_only(self, live_db: Path, tmp_path: Path) -> None:
        # It is a byte-for-byte copy of the `credential` table, so docs/08's mode on the
        # live database is worth nothing if the backup beside it is 0644.
        out = backup.snapshot(live_db, tmp_path / "backups")
        assert stat.S_IMODE(out.stat().st_mode) == 0o600

    def test_survives_an_open_writer(self, live_db: Path, tmp_path: Path) -> None:
        # The reason this is VACUUM INTO and not shutil.copy: the sync writes while the
        # dashboard reads, so the -wal is rarely empty at 02:00.
        writer = connect(live_db)
        _add_credential(writer, "gmail:two")
        try:
            out = backup.snapshot(live_db, tmp_path / "backups")
        finally:
            writer.close()

        conn = sqlite3.connect(out)
        sources = {row[0] for row in conn.execute("SELECT source FROM credential")}
        conn.close()
        assert sources == {"gmail:test", "gmail:two"}

    def test_creates_the_backup_directory(self, live_db: Path, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "backups"
        assert not target.exists()
        backup.snapshot(live_db, target)
        assert target.is_dir()

    def test_a_missing_database_is_a_clean_error(self, tmp_path: Path) -> None:
        with pytest.raises(backup.BackupError, match="no database at"):
            backup.snapshot(tmp_path / "gone.db", tmp_path / "backups")

    def test_refuses_to_overwrite_a_snapshot_of_the_same_second(
        self, live_db: Path, tmp_path: Path
    ) -> None:
        when = datetime(2026, 8, 2, 2, 0, tzinfo=UTC)
        backup.snapshot(live_db, tmp_path / "backups", now=when)
        with pytest.raises(backup.BackupError, match="already exists"):
            backup.snapshot(live_db, tmp_path / "backups", now=when)


# ── verify ────────────────────────────────────────────────────────────────


class TestVerify:
    def test_rejects_a_corrupted_file(self, live_db: Path, tmp_path: Path) -> None:
        out = backup.snapshot(live_db, tmp_path / "backups")
        raw = bytearray(out.read_bytes())
        # Past the 100-byte header, so sqlite3 opens it happily and only
        # integrity_check notices — the exact case a header check would miss.
        raw[4096:8192] = b"\x00" * 4096
        out.write_bytes(bytes(raw))
        assert backup.verify(out) is False

    def test_rejects_something_that_is_not_a_database(self, tmp_path: Path) -> None:
        path = tmp_path / "backglass-20260802-020000.db"
        path.write_text("this is a text file")
        assert backup.verify(path) is False

    def test_rejects_a_missing_file(self, tmp_path: Path) -> None:
        assert backup.verify(tmp_path / "nothing.db") is False


# ── rotate ────────────────────────────────────────────────────────────────


class TestRotate:
    def test_keeps_seven_dailies_and_four_weeklies(self, tmp_path: Path) -> None:
        # Ninety consecutive days of 02:00 snapshots, the state this reaches in a
        # quarter of running. Synthetic names only — rotation reads nothing else.
        directory = tmp_path / "backups"
        start = datetime(2026, 8, 2, 2, 0, tzinfo=UTC)
        for day in range(90):
            _aged(directory, (start - timedelta(days=day)).strftime("%Y%m%d-%H%M%S"))

        deleted = backup.rotate(directory)

        left = sorted(p.name for p in directory.glob("*.db"))
        assert len(left) == 11
        assert len(deleted) == 79
        # The seven most recent survive intact.
        assert "backglass-20260802-020000.db" in left
        assert "backglass-20260727-020000.db" in left
        assert "backglass-20260725-020000.db" not in left
        # Then one per ISO week, newest of each, for four weeks — no week beyond the
        # daily window keeps two files.
        older = [name for name in left if name < "backglass-20260727"]
        weeks = [datetime.strptime(name[10:18], "%Y%m%d").isocalendar()[:2] for name in older]
        assert len(older) == 4
        assert len(set(weeks)) == 4

    def test_is_deterministic(self, tmp_path: Path) -> None:
        directory = tmp_path / "backups"
        start = datetime(2026, 8, 2, 2, 0, tzinfo=UTC)
        for day in range(40):
            _aged(directory, (start - timedelta(days=day)).strftime("%Y%m%d-%H%M%S"))
        first = sorted(p.name for p in directory.glob("*.db"))
        backup.rotate(directory)
        kept = sorted(p.name for p in directory.glob("*.db"))

        assert kept != first
        assert backup.rotate(directory) == []  # a second pass has nothing left to do
        assert sorted(p.name for p in directory.glob("*.db")) == kept

    def test_leaves_a_young_directory_alone(self, tmp_path: Path) -> None:
        directory = tmp_path / "backups"
        for day in range(5):
            _aged(directory, f"2026080{day + 1}-020000")
        assert backup.rotate(directory) == []
        assert len(list(directory.glob("*.db"))) == 5

    def test_ignores_files_that_are_not_snapshots(self, tmp_path: Path) -> None:
        directory = tmp_path / "backups"
        for day in range(20):
            _aged(directory, f"202607{day + 1:02d}-020000")
        stray = directory / "notes.txt"
        stray.write_text("hands off")
        backup.rotate(directory)
        assert stray.exists()

    def test_a_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        assert backup.rotate(tmp_path / "never-created") == []


# ── restore_into ──────────────────────────────────────────────────────────


class TestRestoreInto:
    def test_replaces_the_database_and_clears_stale_wal(
        self, live_db: Path, tmp_path: Path
    ) -> None:
        snap = backup.snapshot(live_db, tmp_path / "backups")
        # Diverge the live database, and leave a -wal describing pages of *this* file.
        conn = connect(live_db)
        _add_credential(conn, "gmail:later")
        conn.close()
        wal = live_db.with_name(live_db.name + "-wal")
        wal.write_bytes(b"stale")

        backup.restore_into(snap, live_db)

        assert not wal.exists()
        conn = sqlite3.connect(live_db)
        sources = {row[0] for row in conn.execute("SELECT source FROM credential")}
        conn.close()
        assert sources == {"gmail:test"}
        assert stat.S_IMODE(live_db.stat().st_mode) == 0o600


# ── the CLI ───────────────────────────────────────────────────────────────


@pytest.fixture
def cli_settings(live_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """`backglass backup` / `restore` pointed at a tmp database and tmp backup dir —
    a run from the checkout must never be able to reach the owner's real ledger."""
    made = Settings(db_path=live_db, backup_dir=tmp_path / "backups")
    monkeypatch.setattr(cli, "get_settings", lambda: made)
    return made


class TestBackupCommand:
    def test_prints_the_snapshot_it_wrote(self, cli_settings: Settings) -> None:
        result = CliRunner().invoke(cli.app, ["backup"])
        assert result.exit_code == 0, result.output
        written = list(cli_settings.backup_dir.glob("backglass-*.db"))
        assert len(written) == 1
        assert written[0].name in result.output
        assert backup.verify(written[0])

    def test_rotates_in_the_same_run(self, cli_settings: Settings) -> None:
        for day in range(1, 21):
            _aged(cli_settings.backup_dir, f"202606{day:02d}-020000")
        result = CliRunner().invoke(cli.app, ["backup"])
        assert result.exit_code == 0, result.output
        assert "rotated out" in result.output
        left = sorted(p.name for p in cli_settings.backup_dir.glob("*.db"))
        # One command, both halves: the new snapshot is there and the pile shrank.
        assert len(left) < 21
        assert left[-1] > "backglass-20260620"

    def test_a_missing_database_exits_non_zero_without_a_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            cli,
            "get_settings",
            lambda: Settings(db_path=tmp_path / "gone.db", backup_dir=tmp_path / "b"),
        )
        result = CliRunner().invoke(cli.app, ["backup"])
        assert result.exit_code == 1
        assert "backup failed" in result.output
        assert "Traceback" not in result.output


class TestRestoreCommand:
    def test_without_yes_it_writes_nothing(
        self, cli_settings: Settings, live_db: Path
    ) -> None:
        snap = backup.snapshot(live_db, cli_settings.backup_dir)
        before = live_db.read_bytes()

        result = CliRunner().invoke(cli.app, ["restore", str(snap)])

        assert result.exit_code == 0, result.output
        assert "would restore" in result.output
        assert "nothing written" in result.output
        assert live_db.read_bytes() == before
        # And no safety copy either: a dry run is a dry run.
        assert list(cli_settings.backup_dir.glob("*.db")) == [snap]

    def test_refuses_a_snapshot_that_fails_integrity(
        self, cli_settings: Settings, live_db: Path, tmp_path: Path
    ) -> None:
        bad = tmp_path / "backglass-20260801-020000.db"
        bad.write_text("not a database")
        before = live_db.read_bytes()

        result = CliRunner().invoke(cli.app, ["restore", str(bad), "--yes"])

        assert result.exit_code == 1
        assert "refusing to restore" in result.output
        assert live_db.read_bytes() == before

    def test_refuses_a_snapshot_that_is_not_there(self, cli_settings: Settings) -> None:
        result = CliRunner().invoke(cli.app, ["restore", "/nowhere/backglass-x.db", "--yes"])
        assert result.exit_code == 1
        assert "no such snapshot" in result.output

    def test_saves_the_current_database_before_swapping(
        self, cli_settings: Settings, live_db: Path
    ) -> None:
        snap = backup.snapshot(
            live_db, cli_settings.backup_dir, now=datetime(2026, 8, 1, 2, 0, tzinfo=UTC)
        )
        conn = connect(live_db)
        _add_credential(conn, "gmail:later")
        conn.close()

        result = CliRunner().invoke(cli.app, ["restore", str(snap), "--yes"])
        assert result.exit_code == 0, result.output

        safety = [p for p in cli_settings.backup_dir.glob("*.db") if p != snap]
        assert len(safety) == 1
        # The safety copy is the pre-restore state — the row the restore just discarded
        # is still reachable, which is what makes the restore itself reversible.
        conn = sqlite3.connect(safety[0])
        sources = {row[0] for row in conn.execute("SELECT source FROM credential")}
        conn.close()
        assert "gmail:later" in sources

        conn = sqlite3.connect(live_db)
        restored = {row[0] for row in conn.execute("SELECT source FROM credential")}
        conn.close()
        assert restored == {"gmail:test"}
