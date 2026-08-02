"""Careful .env editing for `backglass setup`. Phase A2.

The .env is the owner's file — hand-commented, hand-ordered — so the writer's one
job is to change *only* the requested assignments and leave every other byte
alone. A key that exists (even commented-out values keep their comment lines;
only the `KEY=` line itself is rewritten) is replaced in place; a key that does
not is appended under one marker heading at the end. When no .env exists it is
seeded from .env.example first, so the owner ends up with the documented file,
not a two-line orphan.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

_APPENDED_HEADING = "# ── added by `backglass setup` ──"

#: This file holds RESEND_API_KEY, GOOGLE_CLIENT_SECRET, GITHUB_TOKEN, SLACK_TOKEN,
#: CANVAS_TOKEN and MODEL_API_KEY. docs/08 §What this system must never do rules out
#: storing credentials the owner does not control; the same reasoning says the ones they
#: do control are not readable by every other account on the machine. Default umask makes
#: it 0644.
_ENV_MODE = 0o600

_LINE_BREAK = re.compile(r"[\r\n]")


class EnvValueError(ValueError):
    """A value cannot be written as one `KEY=value` line."""


def _restrict(path: Path) -> None:
    """Owner-only, applied *before* the write that puts secrets in the file.

    Order is the whole point. Write-then-chmod publishes every key in the file for the
    length of one syscall pair; any local process polling the path wins that race. So the
    mode is set on the file while it is still empty (or still holding only what it already
    held) and the content lands into an already-private file.

    A filesystem that cannot express the mode degrades rather than aborting `backglass
    setup` — CLAUDE.md rule 5 — but says so, because the owner's next move might be to
    put a real API key here.
    """
    try:
        if stat.S_IMODE(path.stat().st_mode) != _ENV_MODE:
            path.chmod(_ENV_MODE)
    except OSError as exc:
        print(
            f"cannot restrict {path} to {_ENV_MODE:04o} ({exc}) — the API keys in it are "
            f"readable by other accounts on this machine",
            file=sys.stderr,
        )


def set_keys(path: Path, updates: dict[str, str], template: Path | None = None) -> list[str]:
    """Apply `updates` to the env file at `path`. Returns the keys that actually
    changed value (idempotent: writing the same value twice reports nothing)."""
    # Checked before anything is opened, so a bad value cannot leave a half-written .env.
    #
    # Values reach here from connectors/detect.py, which globs over ~/Library and
    # ~/Downloads — the owner does not audit those names, and a directory called
    # "notes\nBOUNDARY_MODE=off" would append a second, entirely unrelated assignment to
    # the file. That is a settings override, not a corrupt path. Refusing is right rather
    # than stripping: a path containing a newline is not the path anyone meant, so writing
    # a silently truncated version of it would configure the wrong source and look like it
    # worked. It is also why nothing here quotes — the loader tolerates quotes but the
    # in-place comparison below is against the raw file text, so emitting them would make
    # every run rewrite every line and break the idempotency contract in the docstring.
    for key, value in updates.items():
        if _LINE_BREAK.search(value):
            raise EnvValueError(
                f"{key} value contains a line break and would inject an extra "
                f"assignment into {path}: {value!r}"
            )

    if not path.exists():
        seed = template.read_text() if template and template.exists() else ""
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _ENV_MODE))
        path.write_text(seed)

    # Also tightens a .env the owner (or an older build) already left at 0644, which is
    # the file that has real keys in it today.
    _restrict(path)
    lines = path.read_text().splitlines()
    changed: list[str] = []
    remaining = dict(updates)
    seen: set[str] = set()

    for i, line in enumerate(lines):
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if not match or match.group(1) not in remaining:
            continue
        # Every occurrence is rewritten, not just the first — a duplicated key
        # would otherwise silently win with the stale value at load time.
        key = match.group(1)
        value = remaining[key]
        seen.add(key)
        if match.group(2) != value:
            lines[i] = f"{key}={value}"
            if key not in changed:
                changed.append(key)
    for key in seen:
        remaining.pop(key)

    if remaining:
        if _APPENDED_HEADING not in lines:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(_APPENDED_HEADING)
        for key, value in remaining.items():
            lines.append(f"{key}={value}")
            changed.append(key)

    path.write_text("\n".join(lines) + "\n")
    return changed
