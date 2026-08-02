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
    assert "backglass auth gmail:personal" in unauthed["gmail:personal"]


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
    assert set(unauthed) == {"canvas", "github", "slack"}
    assert all("backglass sync" in needs for needs in unauthed.values())


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
