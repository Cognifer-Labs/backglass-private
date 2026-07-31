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

import re
from pathlib import Path

_APPENDED_HEADING = "# ── added by `backglass setup` ──"


def set_keys(path: Path, updates: dict[str, str], template: Path | None = None) -> list[str]:
    """Apply `updates` to the env file at `path`. Returns the keys that actually
    changed value (idempotent: writing the same value twice reports nothing)."""
    if not path.exists():
        seed = template.read_text() if template and template.exists() else ""
        path.write_text(seed)

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
