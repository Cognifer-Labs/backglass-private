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

from backglass.config import REPO_ROOT

TEMPLATE_DIR = REPO_ROOT / "launchd" / "templates"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"


class ScheduleError(Exception):
    """A precondition for rendering or installing the launchd jobs was not met."""


def render(repo_dir: Path, uv_bin: str, home: Path) -> dict[str, str]:
    """Filename -> rendered plist XML, one entry per `launchd/templates/*.plist.tmpl`.

    Pure and side-effect free: no filesystem writes, no `launchctl` calls. `install()`
    is the only caller that turns this into real files.
    """
    tokens = {
        "{{REPO_DIR}}": str(repo_dir),
        "{{UV_BIN}}": uv_bin,
        "{{HOME}}": str(home),
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


def install(*, dry_run: bool = False, uv_bin: str | None = None) -> dict[str, str]:
    """Render every template for this machine, then write and `launchctl load` it.

    `dry_run=True` renders and returns without writing or loading anything, so the
    output can be inspected or smoke-tested without touching the real machine.
    """
    rendered = render(REPO_ROOT, uv_bin or find_uv(), Path.home())
    if dry_run:
        return rendered

    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    for filename, text in rendered.items():
        target = LAUNCH_AGENTS_DIR / filename
        target.write_text(text)
        subprocess.run(["launchctl", "load", str(target)], check=False)
    return rendered
