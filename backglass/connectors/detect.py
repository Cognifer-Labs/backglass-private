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

Detection is read-only and shallow (stat calls, one small JSON read, and for a
local SQLite store one `PRAGMA` on a read-only handle), so it is cheap enough to
run on every dashboard render and every doctor pass. It never writes anything —
`backglass setup` is the writer, this is the eyes.

A store is only reported readable if it was actually opened. A stat is not proof:
macOS lets a process without Full Disk Access stat chat.db and still refuses the
open, so `exists()` returns True for a store every read will fail on.

The home directory is injectable so tests probe a fake tree, never the real one.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
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
        _apple_mail(settings, home),
        _instagram(settings, home),
        _obsidian(settings, home),
        _apple("apple_notes", "APPLE_NOTES", settings.apple_notes),
        _apple("apple_reminders", "APPLE_REMINDERS", settings.apple_reminders),
        _apple("apple_contacts", "APPLE_CONTACTS", settings.apple_contacts),
        _apple("apple_calendar", "APPLE_CALENDAR", settings.apple_calendar),
        *_credentialed(settings, authed or set()),
    ]


def actionable(detections: list[Detection]) -> list[Detection]:
    """The rows worth showing someone who asked "what can I hook up?"."""
    return [d for d in detections if d.status in (FOUND, NEEDS_SETUP)]


def unreadable(store: Path) -> str | None:
    """None when `store` opens, else the error a fetch would hit.

    Opened exactly the way the connectors open their stores — `mode=ro` over a URI,
    never `immutable=1`, with the same busy timeout — so that a clean probe means the
    connector will get through rather than merely that a file is present. `PRAGMA
    schema_version` is the cheapest statement that still forces SQLite to open the
    database (and, for a WAL store, its -shm sidecar), which is where a permission
    failure actually surfaces.
    """
    try:
        with closing(sqlite3.connect(f"file:{store}?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA busy_timeout = 2000")
            conn.execute("PRAGMA schema_version").fetchone()
    except sqlite3.Error as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


# ── local stores ─────────────────────────────────────────────────────────


def _verified(source: str, store: Path, env_key: str, label: str) -> Detection:
    """`configured`, downgraded when the configured store will not actually open.

    A path in .env is a claim, not a guarantee: the app it belongs to can be
    uninstalled, the store moved, or the permission revoked long after setup wrote
    the line. Reporting `configured` off the .env value alone is how a source stays
    green on the Sources panel while every sync fails against it.
    """
    if not store.exists():
        return Detection(
            source,
            MISSING,
            env_key=env_key,
            env_value=str(store),
            hint=f"{label} is configured at {store}, which is not there any more",
        )
    error = unreadable(store)
    if error is not None:
        return Detection(
            source,
            NEEDS_SETUP,
            env_key=env_key,
            env_value=str(store),
            hint=f"{label} at {store} will not open ({error})",
        )
    return Detection(source, CONFIGURED, hint=str(store))


def _anki(settings: Settings, home: Path) -> Detection:
    if settings.anki_db_path:
        return _verified(
            "anki", Path(settings.anki_db_path), "ANKI_DB_PATH", "Anki's collection"
        )
    profiles = sorted(
        home.glob("Library/Application Support/Anki2/*/collection.anki2"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not profiles:
        return Detection("anki", MISSING, hint="no Anki profile on this machine")
    newest = profiles[0]
    error = unreadable(newest)
    if error is not None:
        return Detection(
            "anki",
            NEEDS_SETUP,
            env_key="ANKI_DB_PATH",
            env_value=str(newest),
            hint=f"found {newest}, but it will not open ({error})",
        )
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
        return _verified(
            "avorio", Path(settings.avorio_db_path), "AVORIO_DB_PATH", "the Avorio store"
        )
    store = home / "Library/Application Support/Avorio/avorio.db"
    if not store.exists():
        return Detection("avorio", MISSING, hint="Avorio has no local store here")
    error = unreadable(store)
    if error is not None:
        return Detection(
            "avorio",
            NEEDS_SETUP,
            env_key="AVORIO_DB_PATH",
            env_value=str(store),
            hint=f"found {store}, but it will not open ({error})",
        )
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
    """iMessage is the one local store whose file can be seen but not read.

    Full Disk Access is granted per-binary, so `stat` succeeds while `open` is refused —
    an earlier version of this function assumed macOS hid chat.db outright and reported
    `configured` on a store every sync had been failing to read. The permission state is
    therefore established by opening the database, never by its presence.
    """
    pinned = settings.imessage_db_path
    configured = pinned is not None
    store = Path(pinned) if pinned is not None else home / "Library/Messages/chat.db"

    if not store.exists():
        # Absence is not proof of absence either: depending on how macOS refuses, an
        # unapproved process can find the whole Messages directory unlistable, so the
        # store reads as gone when it is merely off limits. Both refusal shapes get
        # the same instruction; only a store that opens is called usable.
        return Detection(
            "imessage",
            MISSING if configured else NEEDS_SETUP,
            env_key="IMESSAGE_DB_PATH",
            env_value=str(store),
            hint=(
                f"no Messages store at the configured path {store}"
                if configured
                else "chat.db is not visible — if Messages is set up on this Mac, "
                "grant Full Disk Access to the program running Backglass (System "
                "Settings → Privacy & Security → Full Disk Access), then rerun setup"
            ),
        )

    error = unreadable(store)
    if error is not None:
        return Detection(
            "imessage",
            NEEDS_SETUP,
            env_key="IMESSAGE_DB_PATH",
            env_value=str(store),
            hint=(
                f"chat.db is present but will not open ({error}) — grant Full Disk "
                "Access to the program running Backglass (System Settings → Privacy & "
                "Security → Full Disk Access). Scheduled runs go through uv, so add "
                "the uv binary as well as the terminal, then rerun setup"
            ),
        )

    if configured:
        # A readable store is not a usable source. IMESSAGE_CHATS is an allowlist and an
        # empty one means the connector reads nothing (see connectors/allowlist.py), so
        # reporting `configured` here would put a green line on the Sources panel above a
        # connector that `doctor` is simultaneously calling failed. That split — detection
        # green, health red — is the same shape as the chat.db permission bug this
        # function was rewritten for.
        if not settings.imessage_chats:
            return Detection(
                "imessage",
                NEEDS_SETUP,
                env_key="IMESSAGE_CHATS",
                env_value="",
                hint=(
                    "readable, but no conversations are monitored yet — open /chats to "
                    "choose from what the sync has found"
                ),
            )
        return Detection("imessage", CONFIGURED, hint=str(store))
    return Detection(
        "imessage",
        FOUND,
        env_key="IMESSAGE_DB_PATH",
        env_value=str(store),
        hint=str(store),
    )


def _apple_mail(settings: Settings, home: Path) -> Detection:
    """Mail.app's store, found by its version directory.

    The permission state is established by opening the Envelope Index, not by the
    directory being visible — the same lesson `_imessage` records. Full Disk Access is
    granted per-binary, so a `stat` can succeed while the open is refused, and reporting
    `configured` off a path in `.env` is how a source stays green on the Sources panel
    while every sync fails against it.
    """
    configured = settings.apple_mail_path
    root = Path(configured) if configured else home / "Library/Mail"

    versions = sorted(root.glob("V*"), key=lambda p: p.name, reverse=True)
    index = next(
        (v / "MailData" / "Envelope Index" for v in versions
         if (v / "MailData" / "Envelope Index").exists()),
        None,
    )
    if index is None:
        return Detection(
            "apple-mail",
            MISSING if configured else NEEDS_SETUP,
            env_key="APPLE_MAIL_PATH",
            env_value=str(root),
            hint=(
                f"no Mail store under the configured path {root}"
                if configured
                else "no Mail index under ~/Library/Mail — either Mail.app is not set up "
                "on this Mac, or Full Disk Access has not been granted to the program "
                "running Backglass (System Settings → Privacy & Security)"
            ),
        )

    error = unreadable(index)
    if error is not None:
        return Detection(
            "apple-mail",
            NEEDS_SETUP,
            env_key="APPLE_MAIL_PATH",
            env_value=str(root),
            hint=(
                f"the Mail index is present but will not open ({error}) — grant Full Disk "
                "Access to the program running Backglass (System Settings → Privacy & "
                "Security → Full Disk Access). Scheduled runs go through uv, so add the "
                "uv binary as well as the terminal"
            ),
        )
    if configured:
        return Detection("apple-mail", CONFIGURED, hint=str(index.parent.parent))
    return Detection(
        "apple-mail",
        FOUND,
        env_key="APPLE_MAIL_PATH",
        env_value=str(root),
        hint=f"Mail store {index.parent.parent.name} at {root}",
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
    """The automation-bridge sources, which cannot be probed cheaply.

    Deliberately NOT opened the way `_verified` opens a local SQLite store. Asking
    Calendar.app for its events costs ~18 seconds on a machine with eleven calendars,
    and detection runs on every dashboard render. `found` here is an offer to configure,
    not a claim that the bridge answers — that claim belongs to `health()`, which the
    doctor and every sync already call, and which reports the Automation prompt properly.
    """
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
                    "set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET (docs/07 "
                    "§Setting up Gmail/Calendar/Drive OAuth), then "
                    "`backglass auth <label>` per account"
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
            bool(
                (settings.canvas_base_url and settings.canvas_token)
                or settings.canvas_ics_url
            ),
            # Both routes named, because on an institution that disables student tokens
            # the first hint is a dead end and reporting only it sends the owner back to
            # a button that will never work.
            "set CANVAS_BASE_URL + CANVAS_TOKEN in .env — or, if your institution "
            "disables student tokens, CANVAS_ICS_URL from Calendar → Calendar Feed",
        ),
    ):
        out.append(
            Detection(name, CONFIGURED) if ready else Detection(name, NEEDS_SETUP, hint=hint)
        )
    return out
