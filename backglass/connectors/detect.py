"""Connector auto-detection. Phase A2.

Every local source lives at a well-known macOS location. The owner should never
have to hunt for a path: this module *finds* the stores and reports, per source,
one of four honest states —

  * ``configured``   — the connector is already set up; nothing to do.
  * ``found``        — the store exists and the exact .env line is ready to write.
  * ``needs_setup``  — the source needs something only the owner can supply
                       (a token, an OAuth consent, a Shortcuts permission), and
                       the hint is the exact next command or step.
  * ``missing``      — nothing to detect; the app is not installed or has no data.

Detection is read-only and shallow (stat calls and one small JSON read), so it is
cheap enough to run on every dashboard render and every doctor pass. It never
writes anything — `backglass setup` is the writer, this is the eyes.

The home directory is injectable so tests probe a fake tree, never the real one.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from backglass.config import Settings

CONFIGURED = "configured"
FOUND = "found"
NEEDS_SETUP = "needs_setup"
MISSING = "missing"


@dataclass(frozen=True)
class Detection:
    source: str
    status: str
    #: The .env assignment `backglass setup` would write, e.g. "ANKI_DB_PATH=…".
    env_key: str | None = None
    env_value: str | None = None
    #: One sentence: where it was found, or the exact next step.
    hint: str = ""

    @property
    def env_line(self) -> str | None:
        if self.env_key is None or self.env_value is None:
            return None
        return f"{self.env_key}={self.env_value}"


def detect_all(
    settings: Settings,
    home: Path | None = None,
    authed: set[str] | None = None,
) -> list[Detection]:
    """`authed` is the set of credential-row sources that have completed auth —
    pass it where a connection is at hand so an already-authorized account reads
    ``configured`` instead of re-prompting forever."""
    home = home or Path.home()
    return [
        _anki(settings, home),
        _avorio(settings, home),
        _imessage(settings, home),
        _instagram(settings, home),
        _obsidian(settings, home),
        _apple("apple_notes", "APPLE_NOTES", settings.apple_notes),
        _apple("apple_reminders", "APPLE_REMINDERS", settings.apple_reminders),
        *_credentialed(settings, authed or set()),
    ]


def actionable(detections: list[Detection]) -> list[Detection]:
    """The rows worth showing someone who asked "what can I hook up?"."""
    return [d for d in detections if d.status in (FOUND, NEEDS_SETUP)]


# ── local stores ─────────────────────────────────────────────────────────


def _anki(settings: Settings, home: Path) -> Detection:
    if settings.anki_db_path:
        return Detection("anki", CONFIGURED, hint=str(settings.anki_db_path))
    profiles = sorted(
        home.glob("Library/Application Support/Anki2/*/collection.anki2"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not profiles:
        return Detection("anki", MISSING, hint="no Anki profile on this machine")
    newest = profiles[0]
    note = f" ({len(profiles)} profiles; newest chosen)" if len(profiles) > 1 else ""
    return Detection(
        "anki",
        FOUND,
        env_key="ANKI_DB_PATH",
        env_value=str(newest),
        hint=f"profile '{newest.parent.name}'{note}",
    )


def _avorio(settings: Settings, home: Path) -> Detection:
    if settings.avorio_db_path:
        return Detection("avorio", CONFIGURED, hint=str(settings.avorio_db_path))
    store = home / "Library/Application Support/Avorio/avorio.db"
    if not store.exists():
        return Detection("avorio", MISSING, hint="Avorio has no local store here")
    return Detection(
        "avorio", FOUND, env_key="AVORIO_DB_PATH", env_value=str(store), hint=str(store)
    )


def _instagram(settings: Settings, home: Path) -> Detection:
    if settings.instagram_export_path:
        return Detection(
            "instagram", CONFIGURED, hint=str(settings.instagram_export_path)
        )
    # Meta's export unzips to a folder that keeps this prefix; newest wins if the
    # owner has requested more than one.
    exports = sorted(
        (
            p
            for p in home.glob("Downloads/instagram-*")
            if p.is_dir()
            and any(
                (p / root).is_dir()
                for root in (
                    "your_instagram_activity/messages/inbox",
                    "messages/inbox",
                )
            )
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not exports:
        return Detection(
            "instagram",
            MISSING,
            hint=(
                "no Meta export found — request one (JSON format) at "
                "accountscenter.instagram.com, unzip into ~/Downloads"
            ),
        )
    return Detection(
        "instagram",
        FOUND,
        env_key="INSTAGRAM_EXPORT_PATH",
        env_value=str(exports[0]),
        hint=f"export '{exports[0].name}' — also set INSTAGRAM_CHATS to the chats to read",
    )


def _imessage(settings: Settings, home: Path) -> Detection:
    if settings.imessage_db_path:
        return Detection("imessage", CONFIGURED, hint=str(settings.imessage_db_path))
    store = home / "Library/Messages/chat.db"
    if store.exists():
        return Detection(
            "imessage",
            FOUND,
            env_key="IMESSAGE_DB_PATH",
            env_value=str(store),
            hint=str(store),
        )
    # macOS hides chat.db from processes without Full Disk Access as if it did not
    # exist — so "absent" usually means "no permission", and saying "missing" would
    # send the owner looking for a file that is right there.
    return Detection(
        "imessage",
        NEEDS_SETUP,
        env_key="IMESSAGE_DB_PATH",
        env_value=str(store),
        hint=(
            "chat.db not visible — grant Full Disk Access to this terminal "
            "(System Settings → Privacy & Security), then rerun setup"
        ),
    )


def _obsidian(settings: Settings, home: Path) -> Detection:
    if settings.obsidian_vault_path:
        return Detection("notes", CONFIGURED, hint=str(settings.obsidian_vault_path))
    registry = home / "Library/Application Support/obsidian/obsidian.json"
    if not registry.exists():
        return Detection("notes", MISSING, hint="Obsidian is not installed")
    try:
        vaults = json.loads(registry.read_text()).get("vaults", {})
    except (ValueError, OSError):
        return Detection("notes", MISSING, hint="Obsidian vault registry unreadable")
    # Defensive on shape, not just on IO: this runs on every dashboard render, so
    # a drifted registry entry must degrade to "missing", never 500 the page.
    paths = [
        Path(v["path"]) for v in vaults.values() if isinstance(v, dict) and v.get("path")
    ]
    existing = [p for p in paths if p.exists()]
    if not existing:
        return Detection("notes", MISSING, hint="Obsidian has no vaults")
    # The registry keeps last-open state per vault; most-recently-modified folder is
    # the closest cheap proxy for "the vault the owner actually uses".
    newest = max(existing, key=lambda p: p.stat().st_mtime)
    note = f" ({len(existing)} vaults; most recent chosen)" if len(existing) > 1 else ""
    return Detection(
        "notes",
        FOUND,
        env_key="OBSIDIAN_VAULT_PATH",
        env_value=str(newest),
        hint=f"vault '{newest.name}'{note}",
    )


def _apple(source: str, env_key: str, enabled: bool) -> Detection:
    if enabled:
        return Detection(source, CONFIGURED)
    if shutil.which("osascript") is None:  # not macOS — nothing to offer
        return Detection(source, MISSING, hint="requires macOS")
    return Detection(
        source,
        FOUND,
        env_key=env_key,
        env_value="1",
        hint="first sync triggers a one-time Automation permission prompt",
    )


# ── token / OAuth sources — detection reports the exact remaining step ────


def _credentialed(settings: Settings, authed: set[str]) -> list[Detection]:
    out: list[Detection] = []

    if not (settings.google_client_id and settings.google_client_secret):
        out.append(
            Detection(
                "google",
                NEEDS_SETUP,
                hint=(
                    "set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET (docs/13 step 3), "
                    "then `backglass auth <label>` per account"
                ),
            )
        )
    else:
        for kind, labels in (
            ("gmail", settings.gmail_accounts),
            ("calendar", settings.calendar_accounts),
            ("drive", settings.drive_accounts),
        ):
            for label in labels:
                name = f"{kind}:{label}"
                if name in authed:
                    out.append(Detection(name, CONFIGURED))
                else:
                    out.append(
                        Detection(
                            name,
                            NEEDS_SETUP,
                            hint=f"run `backglass auth {label} --source {kind}` once",
                        )
                    )

    for name, ready, hint in (
        ("github", bool(settings.github_token), "set GITHUB_TOKEN (a PAT) in .env"),
        (
            "slack",
            bool(settings.slack_token and settings.slack_channels),
            "set SLACK_TOKEN + SLACK_CHANNELS in .env",
        ),
        (
            "canvas",
            bool(settings.canvas_base_url and settings.canvas_token),
            "set CANVAS_BASE_URL + CANVAS_TOKEN in .env",
        ),
    ):
        out.append(
            Detection(name, CONFIGURED) if ready else Detection(name, NEEDS_SETUP, hint=hint)
        )
    return out
