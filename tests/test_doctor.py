"""The doctor's launchd and backup checks — both directions on each.

The Phase 8 verifier refuted the first version of the launchd check: a substring
test went green on the desktop app's transient GUI registration and would have
stayed red forever against the real job labels. Both failure modes are pinned here.

The backup check (audit #23) has three outcomes rather than two — note, ok, fail —
and each one is asserted, because a preflight check that is only tested on its
happy path is a check nobody has seen fail.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from backglass import __main__ as cli
from backglass import backup
from backglass.__main__ import (
    LAUNCHD_LABELS,
    _boundary_verdict,
    _unauthed_remote_sources,
    missing_launchd_jobs,
)
from backglass.config import Settings

#: What `launchctl list` actually prints: PID, exit status, label.
APP_ONLY = """\
PID\tStatus\tLabel
612\t0\tcom.apple.Finder
25004\t0\tapplication.com.backglass.desktop.125004010.125004852
"""

ALL_INSTALLED = APP_ONLY + "".join(
    f"-\t0\t{label}\n" for label in LAUNCHD_LABELS
)

PARTIAL = APP_ONLY + "-\t0\tcom.backglass.sync\n"


def test_the_running_app_is_not_a_scheduled_job() -> None:
    # The false green the verifier caught: the Tauri app's GUI registration
    # contains "com.backglass" but schedules nothing.
    assert missing_launchd_jobs(APP_ONLY) == list(LAUNCHD_LABELS)


def test_correctly_installed_jobs_pass() -> None:
    # The false fail: the desktop app's own transient label must not satisfy this.
    assert missing_launchd_jobs(ALL_INSTALLED) == []


def test_partial_install_names_what_is_missing() -> None:
    missing = missing_launchd_jobs(PARTIAL)
    assert "com.backglass.sync" not in missing
    assert "com.backglass.brief" in missing


def test_a_missing_backup_job_is_reported_like_any_other() -> None:
    # Audit #23 put com.backglass.backup in the required set: an owner whose only copy
    # of the record has no scheduled snapshot should see that in doctor, not discover it.
    assert "com.backglass.backup" in missing_launchd_jobs(PARTIAL)


# ── the backup freshness check, both branches (backup.freshness) ──────────


def _snapshot_named(directory, stamp: str):
    path = directory / f"backglass-{stamp}.db"
    path.write_bytes(b"")
    return path


def test_freshness_notes_rather_than_fails_a_fresh_install(tmp_path) -> None:
    # Nothing backed up yet is loud but not red — a clone that has never run `backglass
    # backup` has not broken anything.
    level, detail = backup.freshness(tmp_path, job_installed=True)
    assert level == "note"
    assert "only copy of the record" in detail


def test_freshness_passes_a_recent_snapshot(tmp_path) -> None:
    now = datetime(2026, 8, 2, 9, 0, tzinfo=UTC)
    _snapshot_named(tmp_path, "20260802-020000")
    level, _ = backup.freshness(tmp_path, job_installed=True, now=now)
    assert level == "ok"


def test_freshness_fails_a_stale_snapshot_when_the_job_is_loaded(tmp_path) -> None:
    now = datetime(2026, 8, 2, 9, 0, tzinfo=UTC)
    _snapshot_named(tmp_path, "20260730-020000")  # 79h
    level, detail = backup.freshness(tmp_path, job_installed=True, now=now)
    assert level == "fail"
    assert "79h old" in detail
    assert "com.backglass.backup" in detail


def test_a_stale_snapshot_without_the_job_is_not_a_second_red(tmp_path) -> None:
    # The missing job is already the failing check; saying it twice trains the owner to
    # skim doctor output.
    now = datetime(2026, 8, 2, 9, 0, tzinfo=UTC)
    _snapshot_named(tmp_path, "20260730-020000")
    level, _ = backup.freshness(tmp_path, job_installed=False, now=now)
    assert level == "ok"


def test_labels_match_the_shipped_plist_templates() -> None:
    # The constant and launchd/templates/*.plist.tmpl must never drift apart.
    from pathlib import Path

    from backglass.config import REPO_ROOT

    plist_labels = set()
    for plist in Path(REPO_ROOT, "launchd", "templates").glob("*.plist.tmpl"):
        text = plist.read_text()
        for label in LAUNCHD_LABELS:
            if f"<string>{label}</string>" in text:
                plist_labels.add(label)
    assert plist_labels == set(LAUNCHD_LABELS)


# ── configured-but-never-authed sources ──────────────────────────────────
#
# A source with no credential row is invisible to every other doctor check: the health
# loop iterates credentials, so a Gmail account named in .env that never finished OAuth
# reads exactly like one nobody ever asked for. Both directions pinned.


def _cfg(**over: object) -> Settings:
    return Settings(_env_file=None, **over)  # type: ignore[call-arg,arg-type]


def test_a_configured_gmail_account_that_never_authed_is_named() -> None:
    settings = _cfg(gmail_accounts=["personal", "school"])
    unauthed = dict(_unauthed_remote_sources(settings, known=set()))
    assert set(unauthed) == {"gmail:personal", "gmail:school"}
    # The hint must be runnable as printed. `auth` takes a bare label plus --source and
    # stores under f"{source}:{account}", so `auth gmail:personal` would write
    # gmail:gmail:personal — a row no connector loads, leaving doctor printing the same
    # line forever. detect.py already prints this spelling; both must agree.
    assert unauthed["gmail:personal"] == "run `backglass auth personal --source gmail`"


def test_an_authed_account_is_not_reported() -> None:
    # The false positive that would make this line noise: a source with a credential
    # row is already covered by the health loop and must not be listed twice.
    settings = _cfg(gmail_accounts=["personal"], drive_accounts=["personal"])
    unauthed = dict(_unauthed_remote_sources(settings, known={"gmail:personal"}))
    assert set(unauthed) == {"drive:personal"}


def test_token_sources_need_a_sync_not_an_auth_flow() -> None:
    # Canvas/GitHub/Slack have no OAuth dance — their credential row appears on the
    # first sync, so telling the owner to "auth" them would send them nowhere.
    settings = _cfg(
        canvas_base_url="https://example.instructure.com",
        canvas_token="t",
        github_token="t",
        slack_token="t",
        slack_channels=["general"],
    )
    unauthed = dict(_unauthed_remote_sources(settings, known=set()))
    # source:label, exactly as the connectors register — a bare "canvas" would never
    # match the canvas:canvas credential row a synced Canvas writes, so a working
    # source would be reported as never authed on every doctor run forever.
    assert set(unauthed) == {"canvas:canvas", "github:personal", "slack:personal"}
    assert all("backglass sync" in needs for needs in unauthed.values())


def test_a_synced_token_source_stops_being_reported() -> None:
    # The regression the names above exist for: the credential row a Canvas sync writes
    # is named after the connector, and doctor has to recognise it as the same source.
    settings = _cfg(canvas_base_url="https://example.instructure.com", canvas_token="t")
    assert _unauthed_remote_sources(settings, known={"canvas:canvas"}) == []


def test_a_half_configured_token_source_is_not_reported() -> None:
    # A token with no base URL (or a Slack token with no channels) cannot sync at all;
    # reporting it as "never authed" would blame the wrong missing piece.
    settings = _cfg(canvas_token="t", slack_token="t")
    assert _unauthed_remote_sources(settings, known=set()) == []


def test_nothing_configured_reports_nothing() -> None:
    assert _unauthed_remote_sources(_cfg(), known=set()) == []


# ── the data boundary (docs/08, CLAUDE.md rule 6) ────────────────────────
#
# The check that exists because the machinery was correct and the deployment was inert
# for weeks (audit #18). Silent until it applies, red until the owner decides.


def test_no_boundary_scoped_source_means_no_verdict() -> None:
    # Local stores are the owner's own writing — asking about a client-data boundary
    # over Apple Notes would be noise, and noise is how a legal-weight check gets skimmed.
    settings = _cfg(boundary_mode="exclude")
    creds = [("apple-notes", True), ("reminders", True), ("imessage", True)]
    assert _boundary_verdict(settings, creds) is None


def test_an_enabled_mail_source_with_an_empty_denylist_fails() -> None:
    settings = _cfg(boundary_mode="exclude")
    ok, detail = _boundary_verdict(settings, [("gmail:personal", True)])  # type: ignore[misc]
    assert not ok
    assert "gmail" in detail
    assert "full_scope" in detail


def test_a_populated_denylist_passes() -> None:
    settings = _cfg(boundary_mode="exclude", boundary_deny_domains=["client.example"])
    ok, _ = _boundary_verdict(settings, [("gmail:personal", True)])  # type: ignore[misc]
    assert ok


def test_full_scope_is_the_other_legitimate_answer() -> None:
    # docs/08 Option B: ingesting everything is allowed, provided it was chosen.
    settings = _cfg(boundary_mode="full_scope")
    ok, _ = _boundary_verdict(settings, [("drive:personal", True)])  # type: ignore[misc]
    assert ok


def test_a_paused_source_does_not_trigger_the_check() -> None:
    # Nothing is being ingested through it, so there is nothing to decide yet.
    settings = _cfg(boundary_mode="exclude")
    assert _boundary_verdict(settings, [("gmail:personal", False)]) is None


def test_the_detail_names_every_live_scoped_source_once() -> None:
    settings = _cfg(boundary_mode="exclude")
    creds = [("gmail:personal", True), ("gmail:school", True), ("slack", True)]
    ok, detail = _boundary_verdict(settings, creds)  # type: ignore[misc]
    assert not ok
    assert detail.split(" enabled")[0] == "gmail, slack"


# ── defects the fresh-context verifier found in the first version ────────
#
# Each of these reproduces a scenario that was live on the owner's machine or one
# command away from it. They are written as the verifier posed them.


def test_ingested_data_with_no_credential_still_demands_a_boundary_decision() -> None:
    """The one that was live: 200 calendar:asu items, no credential row, check silent.

    Deriving "is a scoped source live" from the credential table alone missed an entire
    class — data imported or left behind by a removed connector — which is exactly the
    class the unmanaged-sources line was added to surface in the same commit range.
    Data already in the ledger is the strongest reason to have decided, not a weaker one.
    """
    settings = _cfg(boundary_mode="exclude")
    creds = [("apple-notes", True), ("reminders", True)]
    verdict = _boundary_verdict(settings, creds, ingested=["calendar:asu", "apple-notes"])
    assert verdict is not None
    ok, detail = verdict
    assert not ok
    assert "calendar" in detail


def test_an_ingested_source_is_not_double_counted_with_its_credential() -> None:
    settings = _cfg(boundary_mode="exclude")
    ok, detail = _boundary_verdict(  # type: ignore[misc]
        settings, [("gmail:personal", True)], ingested=["gmail:personal", "gmail:school"]
    )
    assert not ok
    assert detail.split(" enabled")[0] == "gmail"


def test_ingested_local_sources_still_mean_no_verdict() -> None:
    settings = _cfg(boundary_mode="exclude")
    assert (
        _boundary_verdict(settings, [], ingested=["apple-notes", "anki", "manual"])
        is None
    )


def test_a_malformed_snapshot_filename_does_not_take_doctor_down(tmp_path) -> None:
    """February 31st, from a hand-copy or a skewed clock, used to raise ValueError out
    of snapshots() into both rotate() and freshness() — bricking doctor and stopping
    rotation forever while backup kept appending."""
    _snapshot_named(tmp_path, "20260231-020000")  # no such date
    _snapshot_named(tmp_path, "20260802-020000")
    now = datetime(2026, 8, 2, 9, 0, tzinfo=UTC)

    level, detail = backup.freshness(tmp_path, job_installed=True, now=now)

    assert level == "ok"
    assert "20260802" in detail
    assert backup.rotate(tmp_path) == []  # and rotation still runs


def test_a_future_dated_snapshot_cannot_mask_a_stale_one(tmp_path) -> None:
    """A clock-skewed snapshot sorts first, yields a negative age, and reads as fresh
    forever — a permanent green check over a genuinely stale backup."""
    _snapshot_named(tmp_path, "20991231-235959")
    _snapshot_named(tmp_path, "20260101-000000")  # 7 months old
    now = datetime(2026, 8, 2, 9, 0, tzinfo=UTC)

    level, detail = backup.freshness(tmp_path, job_installed=True, now=now)

    assert level == "fail"
    assert "20260101" in detail


def test_only_future_dated_snapshots_fail_loudly(tmp_path) -> None:
    _snapshot_named(tmp_path, "20991231-235959")
    now = datetime(2026, 8, 2, 9, 0, tzinfo=UTC)
    level, detail = backup.freshness(tmp_path, job_installed=True, now=now)
    assert level == "fail"
    assert "future" in detail


# ── the command itself, not only its helpers ─────────────────────────────
#
# Every check above is a pure function, which is why they are testable — but the wiring
# that decides *which* branch runs had no test at all, and "a check nobody has seen fail
# is a check nobody has seen work" applies to the wiring too.


@pytest.fixture
def doctor_env(tmp_path, monkeypatch):
    """`backglass doctor` against a tmp database and tmp backup dir, with launchctl and
    the connector probes stubbed — never the owner's real ledger or real launchd."""
    from backglass.db import connect, migrate

    db = tmp_path / "backglass.db"
    conn = connect(db)
    migrate(conn)
    conn.close()
    made = Settings(
        db_path=db,
        backup_dir=tmp_path / "backups",
        owner_emails=["a@example.com"],
        model_backend="anthropic",
        model_api_key="k",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: made)
    monkeypatch.setattr(cli, "_all_connectors", lambda conn, settings: [])
    monkeypatch.setattr(
        "backglass.connectors.detect.detect_all", lambda settings, authed=(): []
    )

    loaded: list[str] = []

    class _Done:
        returncode = 0
        stdout = ""

    def fake_run(cmd, **kw):
        done = _Done()
        if cmd[:2] == ["launchctl", "list"]:
            done.stdout = "".join(f"-\t0\t{label}\n" for label in loaded)
        return done

    monkeypatch.setattr("subprocess.run", fake_run)
    return made, loaded


def _doctor(monkeypatch) -> str:
    return CliRunner().invoke(cli.app, ["doctor"]).output


def test_doctor_notes_a_never_run_sync_rather_than_failing_it(doctor_env, monkeypatch) -> None:
    # A fresh install has never synced; that is a note, not a red — there is nothing
    # broken yet, and a red on first run teaches the owner to ignore reds.
    out = _doctor(monkeypatch)
    assert "sync has never run" in out
    assert "sync is running on schedule" not in out


def test_doctor_stays_quiet_about_staleness_when_the_sync_job_is_not_loaded(
    doctor_env, monkeypatch
) -> None:
    # The missing-job check is already the red. Two reds for one cause trains skimming.
    settings, _loaded = doctor_env
    conn = cli._open(settings)
    conn.execute(
        "INSERT INTO run (user_id, kind, started_at, finished_at) "
        "VALUES (1, 'sync', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    out = _doctor(monkeypatch)

    assert "launchd jobs loaded" in out
    assert "sync is running on schedule" not in out


def test_doctor_fails_a_stale_sync_once_the_job_is_loaded(doctor_env, monkeypatch) -> None:
    settings, loaded = doctor_env
    loaded.extend(LAUNCHD_LABELS)
    conn = cli._open(settings)
    conn.execute(
        "INSERT INTO run (user_id, kind, started_at, finished_at) "
        "VALUES (1, 'sync', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    out = _doctor(monkeypatch)

    assert "[FAIL] sync is running on schedule" in out


def test_doctor_notes_a_missing_backup_and_fails_a_stale_one(doctor_env, monkeypatch) -> None:
    settings, loaded = doctor_env
    loaded.extend(LAUNCHD_LABELS)

    assert "no ledger snapshot" in _doctor(monkeypatch)

    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    _snapshot_named(settings.backup_dir, "20200101-020000")
    assert "[FAIL] ledger backup is fresh" in _doctor(monkeypatch)


def test_a_hand_imports_own_spelling_still_demands_a_boundary_decision() -> None:
    """The next gap in D1's class, found by re-verification.

    The rows that made D1 real exist *because of a hand import*, and a hand import picks
    its own spelling. An unnormalized `source.split(":")[0] in BOUNDARY_SCOPED` was
    silent on every one of these — the same blind spot in a new coat.
    """
    settings = _cfg(boundary_mode="exclude")
    for spelling in (
        "Calendar:ASU",
        "CALENDAR:asu",
        "  calendar:asu ",
        "gcal:asu",
        "google-calendar",
    ):
        verdict = _boundary_verdict(settings, [], ingested=[spelling])
        assert verdict is not None, spelling
        assert not verdict[0], spelling
        assert "calendar" in verdict[1], spelling


def test_an_unrelated_source_is_still_not_scoped() -> None:
    # The mirror direction: normalization must not start matching things it shouldn't.
    settings = _cfg(boundary_mode="exclude")
    for spelling in ("apple-notes", "anki", "calendarific", "manual"):
        assert _boundary_verdict(settings, [], ingested=[spelling]) is None, spelling
