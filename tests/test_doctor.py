"""The doctor's launchd check — both directions, on realistic launchctl output.

The Phase 8 verifier refuted the first version: a substring test went green on
the desktop app's transient GUI registration and would have stayed red forever
against the real job labels. Both failure modes are pinned here.
"""

from __future__ import annotations

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

PARTIAL = APP_ONLY + "-\t0\tcom.cognifer.backglass.sync\n"


def test_the_running_app_is_not_a_scheduled_job() -> None:
    # The false green the verifier caught: the Tauri app's GUI registration
    # contains "com.backglass" but schedules nothing.
    assert missing_launchd_jobs(APP_ONLY) == list(LAUNCHD_LABELS)


def test_correctly_installed_jobs_pass() -> None:
    # The false fail: the real labels are com.cognifer.backglass.*.
    assert missing_launchd_jobs(ALL_INSTALLED) == []


def test_partial_install_names_what_is_missing() -> None:
    missing = missing_launchd_jobs(PARTIAL)
    assert "com.cognifer.backglass.sync" not in missing
    assert "com.cognifer.backglass.brief" in missing


def test_labels_match_the_shipped_plists() -> None:
    # The constant and launchd/*.plist must never drift apart.
    from pathlib import Path

    from backglass.config import REPO_ROOT

    plist_labels = set()
    for plist in Path(REPO_ROOT, "launchd").glob("*.plist"):
        text = plist.read_text()
        for label in LAUNCHD_LABELS:
            if f"<string>{label}</string>" in text:
                plist_labels.add(label)
    assert plist_labels == set(LAUNCHD_LABELS)
