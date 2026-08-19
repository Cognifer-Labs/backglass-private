"""Keep the installed app level with the checkout, without anyone remembering to.

The desktop app is a frozen PyInstaller sidecar inside a Tauri bundle, so every change
to a template, a stylesheet or a Python module leaves the running app behind the code
until someone rebuilds and reinstalls it by hand. Nothing announced that gap: on
2026-08-16 the installed app was running the previous week's planner, and `state` said
`matches_source: True` because it had never hashed the frozen Python.

`state` can now see the drift. This closes it, on a timer, and the shape is deliberately
the smallest thing that works for one machine:

  - **No feed, no keys, no versions.** A hosted updater (Tauri's, or Sparkle) is the
    right answer for shipping to other people, and it needs published artifacts, a
    signing key and a version bumped per release. None of that exists here, and none of
    it would make the owner's own app any fresher than a rebuild from the checkout it is
    already sitting next to.
  - **It only ever builds what the bundle proves is stale.** The comparison is the one
    `state` already makes: hashes of the frozen surfaces and the Python manifest against
    the working tree. Equal means no build, and a build is nine minutes of CPU, so this
    must never run on a hunch.
  - **It refuses a dirty tree.** Installing uncommitted work would put code into
    /Applications that exists nowhere else, and "what is the app running?" would stop
    having an answer anyone can check out.
  - **It never interrupts the app.** A rebuild while the owner has Backglass open
    installs nothing; it says so and waits for the next run, because the app is killed
    on window close anyway and a self-quitting app is a worse surprise than a day-old
    one. `--now` is the explicit override, and it quits and relaunches on purpose.

The job that drives it is `StartInterval`, not `StartCalendarInterval`. Today's lesson,
written down where it applies: a calendar job on this machine has been firing on the
timezone the Mac booted in for weeks, and an interval counts seconds, which are the same
in every zone.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from backglass import state as state_mod
from backglass.config import REPO_ROOT

#: The build script owns the whole rebuild: freeze, smoke test, stage, manifest, bundle,
#: sign. This module decides *whether* to call it and what to do with what it produces.
BUILD_SCRIPT = REPO_ROOT / "desktop" / "build-sidecar.sh"
BUNDLE = REPO_ROOT / "desktop/src-tauri/target/release/bundle/macos/Backglass.app"

#: Kept beside the app rather than deleted: a rebuild that turns out to be broken should
#: be one `ditto` away from the copy that worked.
BACKUP_DIR = Path.home() / "Library/Application Support/Backglass/app-backups"


@dataclass(frozen=True)
class Decision:
    """Whether to rebuild, and the sentence explaining it either way.

    The reason is not decoration. This runs unattended on a timer, so the log line is
    the only account of why nine minutes of CPU were or were not spent, and "nothing to
    do" and "refused" must never read the same.
    """

    build: bool
    reason: str
    stale: tuple[str, ...] = ()


def installed_is_current(app: Path | None = None) -> tuple[bool, tuple[str, ...], str | None]:
    """Does the installed bundle match this checkout? Returns (current, stale, note).

    Reuses `state`'s comparison rather than restating it. Two copies of "is the app
    stale" would drift, and the one that drifted would be this one — it runs unattended,
    where a wrong answer is silent.
    """
    target = app or state_mod.INSTALLED_APP
    if not target.exists():
        return False, (), "not installed"
    frozen_root = target / "Contents/Resources/sidecar/backglass-server/_internal"
    stale: list[str] = []
    for relative in state_mod.frozen_surfaces():
        theirs = state_mod._sha256(frozen_root / relative)
        ours = state_mod._sha256(REPO_ROOT / relative)
        if theirs is None or ours is None or theirs != ours:
            stale.append(relative)
    drifted, note = state_mod._stale_python(target)
    stale.extend(drifted)
    return (not stale and note is None), tuple(stale), note


def working_tree_is_clean(repo: Path | None = None) -> tuple[bool, str]:
    """Nothing uncommitted. A build installs whatever is on disk, and an app built from
    an uncommitted tree cannot be checked out, diffed or gone back to."""
    root = repo or REPO_ROOT
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root, capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return False, f"git status failed: {exc}"
    dirty = [line for line in result.stdout.splitlines() if line.strip()]
    if dirty:
        return False, f"{len(dirty)} uncommitted change(s)"
    return True, "clean"


def app_is_running(app: Path | None = None) -> bool:
    """Is the owner looking at it right now? `pgrep -f` against the installed path, so a
    dev server started from the checkout does not count as the app being open."""
    target = app or state_mod.INSTALLED_APP
    try:
        found = subprocess.run(
            ["pgrep", "-f", f"{target}/Contents/MacOS/"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return False
    return bool(found.stdout.strip())


def decide(app: Path | None = None, repo: Path | None = None) -> Decision:
    """Build or not, with the reason. Pure enough to test: it only reads."""
    current, stale, note = installed_is_current(app)
    target = app or state_mod.INSTALLED_APP
    if not target.exists():
        # Installing the first copy is a human act — it decides where the app lives.
        return Decision(False, "no app installed; nothing to keep current")
    if current:
        return Decision(False, "installed app matches the checkout")

    clean, detail = working_tree_is_clean(repo)
    if not clean:
        return Decision(
            False,
            f"stale, but the working tree has {detail} — commit them and this will "
            f"rebuild on its next run",
            stale,
        )
    if note:
        return Decision(True, note, stale)
    return Decision(True, f"{len(stale)} frozen surface(s) differ from the checkout", stale)


# ── doing it ──────────────────────────────────────────────────────────────────


@dataclass
class Result:
    """What a run did, in the order it did it. Every line is printed by the caller and
    lands in the job's log, because an unattended rebuild that says nothing is
    indistinguishable from one that never ran — the failure this whole file is about."""

    decision: Decision
    built: bool = False
    installed: bool = False
    backup: Path | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """Non-zero only for a build or install that was attempted and failed. A skip is
        a designed outcome, not an error: an hourly job that exits 2 because the owner has
        the app open teaches its log to be ignored."""
        return 1 if any(n.startswith("failed") for n in self.notes) else 0


#: One rebuild at a time. flock releases on process death, so a machine that slept
#: mid-build cannot strand it — the same reasoning as `sync.run_lock`.
LOCK_PATH = REPO_ROOT / "data" / "app-build.lock"


class BuildLocked(RuntimeError):
    """Another rebuild holds the lock."""


def _build(runner: Callable[[list[str], Path], int]) -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise BuildLocked("another rebuild is already running") from None
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid {os.getpid()} since {datetime.now().isoformat(timespec='seconds')}")
        handle.flush()
        code = runner([str(BUILD_SCRIPT)], REPO_ROOT)
        if code != 0:
            raise RuntimeError(f"build-sidecar.sh exited {code}")
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _shell(command: list[str], cwd: Path) -> int:
    return subprocess.run(["bash", *command], cwd=cwd, check=False).returncode


def _install(app: Path, bundle: Path, runner: Callable[[list[str], Path], int]) -> Path | None:
    """Replace the installed app, keeping the copy that was working.

    Order matters and is the whole of the safety here: verify the new bundle exists and
    passes `codesign` *before* the old one is touched, then copy the old one aside, then
    swap. A failure at any step leaves something launchable in /Applications.
    """
    if not bundle.exists():
        raise RuntimeError(f"no bundle at {bundle}")
    if runner(["-c", f'codesign --verify --strict "{bundle}"'], REPO_ROOT) != 0:
        raise RuntimeError("the freshly built bundle does not pass codesign")

    backup: Path | None = None
    if app.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = BACKUP_DIR / f"Backglass-{stamp}.app"
        if runner(["-c", f'ditto "{app}" "{backup}"'], REPO_ROOT) != 0:
            raise RuntimeError("could not back up the installed app; nothing replaced")
        if runner(["-c", f'rm -rf "{app}"'], REPO_ROOT) != 0:
            raise RuntimeError("could not remove the installed app")
    if runner(["-c", f'ditto "{bundle}" "{app}"'], REPO_ROOT) != 0:
        raise RuntimeError(f"install failed; the previous app is at {backup}")
    return backup


def run(
    *,
    now: bool = False,
    check_only: bool = False,
    app: Path | None = None,
    repo: Path | None = None,
    runner: Callable[[list[str], Path], int] | None = None,
) -> Result:
    """Decide, and if warranted rebuild and install. Never raises; the Result says what
    happened, because this is called from a launchd job whose only voice is its log.

    `now=True` is the owner asking for it: it quits a running app, installs, and starts
    it again. Without it a running app defers the install to the next run, which on an
    app that dies with its window is usually minutes away.
    """
    call = runner or _shell
    decision = decide(app, repo)
    result = Result(decision=decision)
    if check_only or not decision.build:
        return result

    target = app or state_mod.INSTALLED_APP
    running = app_is_running(target)
    if running and not now:
        result.notes.append(
            "deferred: Backglass is open, and an app that quits itself under the owner "
            "is a worse surprise than a day-old one. It installs on the next run."
        )
        return result

    try:
        _build(call)
        result.built = True
    except BuildLocked as exc:
        result.notes.append(f"skipped: {exc}")
        return result
    except Exception as exc:  # noqa: BLE001 - the log is the only reader
        result.notes.append(f"failed to build: {exc}")
        return result

    if running:
        call(["-c", 'osascript -e \'tell application "Backglass" to quit\''], REPO_ROOT)
        result.notes.append("quit the running app")
    try:
        result.backup = _install(target, BUNDLE, call)
        result.installed = True
    except Exception as exc:  # noqa: BLE001 - same
        result.notes.append(f"failed to install: {exc}")
        return result
    if running:
        call(["-c", f'open -a "{target}"'], REPO_ROOT)
        result.notes.append("relaunched it")
    return result
