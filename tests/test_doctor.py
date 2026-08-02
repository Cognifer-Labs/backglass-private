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

from backglass import backup
from backglass.__main__ import LAUNCHD_LABELS, missing_launchd_jobs

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
