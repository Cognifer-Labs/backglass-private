"""launchd job scheduling. docs/10 §Scheduling: files, not an in-process scheduler.

The six jobs used to ship as static `launchd/*.plist` files with one machine's absolute
paths baked in (e.g. `/Users/example/backglass`, `/Users/example/.local/bin/uv`) —
which meant they were never actually installable anywhere else. They now live as
templates at `launchd/templates/*.plist.tmpl` with `{{REPO_DIR}}`, `{{UV_BIN}}`, and
`{{HOME}}` placeholders. `render()` fills those in for whichever machine is running this;
`install()` writes the rendered files to `~/Library/LaunchAgents` and loads them.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from backglass.config import REPO_ROOT, Settings

TEMPLATE_DIR = REPO_ROOT / "launchd" / "templates"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"


class ScheduleError(Exception):
    """A precondition for rendering or installing the launchd jobs was not met."""


def _hhmm(value: str) -> tuple[int, int]:
    hour, _, minute = value.partition(":")
    return int(hour), int(minute)


def shutdown_time(settings: Settings) -> tuple[int, int]:
    """When the evening pass should run: the end of the working window.

    docs/04 §1.8 calls shutdown "a second, much smaller pass at the end of the working
    window", and the template had 18:00 frozen into it from the 09:00–18:00 default.
    The owner's window is 10:00–22:00, so the job was asking what got done with a third
    of the day still ahead of it — and, since the gym routine starts at 17:30, asking it
    of an empty chair. A time the owner configures in one place should not need to be
    remembered in a second.

    No clamp for a midnight end, because `Settings` cannot hold one: the
    `working_window` validator rejects anything but `HH:MM-HH:MM` with a real hour, so
    "10:00-24:00" never reaches here and a branch guarding against it would be a branch
    no test could reach.
    """
    _, _, end = settings.working_window.partition("-")
    return _hhmm(end)


def render(
    repo_dir: Path, uv_bin: str, home: Path, settings: Settings | None = None
) -> dict[str, str]:
    """Filename -> rendered plist XML, one entry per `launchd/templates/*.plist.tmpl`.

    Pure and side-effect free: no filesystem writes, no `launchctl` calls. `install()`
    is the only caller that turns this into real files.

    The two owner-facing fire times come from settings rather than the templates. A
    schedule is a claim about when things happen, and a claim frozen in a file the
    owner never edits stops being true the first time they change `BRIEF_AT` or
    `WORKING_WINDOW` — silently, because nothing compares the two.
    """
    settings = settings or Settings()
    brief_hour, brief_minute = _hhmm(settings.brief_at)
    plan_hour, plan_minute = _hhmm(settings.plan_at)
    shutdown_hour, shutdown_minute = shutdown_time(settings)
    tokens = {
        "{{REPO_DIR}}": str(repo_dir),
        "{{UV_BIN}}": uv_bin,
        "{{HOME}}": str(home),
        "{{BRIEF_HOUR}}": str(brief_hour),
        "{{BRIEF_MINUTE}}": str(brief_minute),
        "{{PLAN_HOUR}}": str(plan_hour),
        "{{PLAN_MINUTE}}": str(plan_minute),
        "{{SHUTDOWN_HOUR}}": str(shutdown_hour),
        "{{SHUTDOWN_MINUTE}}": str(shutdown_minute),
    }
    templates = sorted(TEMPLATE_DIR.glob("*.plist.tmpl"))
    if not templates:
        raise ScheduleError(f"no *.plist.tmpl files found under {TEMPLATE_DIR}")
    rendered: dict[str, str] = {}
    for template in templates:
        text = template.read_text()
        for token, value in tokens.items():
            text = text.replace(token, value)
        rendered[template.name.removesuffix(".tmpl")] = text
    return rendered


def find_uv() -> str:
    found = shutil.which("uv")
    if not found:
        raise ScheduleError(
            "uv not found on PATH — install it (https://docs.astral.sh/uv/) before "
            "scheduling launchd jobs, or pass an explicit uv path"
        )
    return found


#: The overnight Batches-API jobs. They need a real Anthropic API key — the
#: subscription CLI backend cannot drive them — so they are conditional where every
#: other job is not.
BATCH_PREFIX = "com.backglass.batch-"


def install(
    *, dry_run: bool = False, uv_bin: str | None = None, batch_lane: bool = True
) -> dict[str, str]:
    """Render every template for this machine, then write and `launchctl load` it.

    `dry_run=True` renders and returns without writing or loading anything, so the
    output can be inspected or smoke-tested without touching the real machine.

    `batch_lane=False` means this machine has no Anthropic API key: the batch-submit
    and batch-collect jobs are skipped, and any previously installed copies are
    unloaded and removed. A job whose every fire can only exit 2 is not a schedule,
    it is a nightly error log — and extraction is covered by the sync path anyway.
    """
    rendered = render(REPO_ROOT, uv_bin or find_uv(), Path.home())
    skipped = (
        [] if batch_lane else [f for f in rendered if f.startswith(BATCH_PREFIX)]
    )
    for filename in skipped:
        rendered.pop(filename)
    if dry_run:
        return rendered

    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    for filename, text in rendered.items():
        target = LAUNCH_AGENTS_DIR / filename
        target.write_text(text)
        # Unload first, always. `launchctl load` on a job that is already loaded does
        # not reload it — it fails with "Load failed: 5: Input/output error" and leaves
        # the previously registered definition running. So on any machine that has ever
        # installed these jobs, which is every machine that would run this command,
        # writing a new plist changed the file and nothing else.
        #
        # Found by moving the shutdown hour to 22:00: the file on disk said 22, and
        # `launchctl print` said the live job was still firing at 18. A schedule command
        # whose only observable effect is on a file nobody reads is worse than none,
        # because `installed …` printed for every job either way.
        #
        # `check=False` on the unload because not-loaded is the ordinary first-run case
        # and not an error.
        subprocess.run(["launchctl", "unload", str(target)], check=False)
        subprocess.run(["launchctl", "load", str(target)], check=False)
    for filename in skipped:
        target = LAUNCH_AGENTS_DIR / filename
        if target.exists():
            subprocess.run(["launchctl", "unload", str(target)], check=False)
            target.unlink()
    return rendered
