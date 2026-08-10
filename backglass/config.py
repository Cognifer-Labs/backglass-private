"""Settings, loaded from .env. See .env.example for the documented surface.

Only app-level credentials live here. Per-user OAuth tokens live in the `credential`
table, per docs/07 §Credentials.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _repo_root() -> Path:
    """Where the runtime data files live (specs/, design/).

    Three homes, in precedence order: an explicit override, the PyInstaller
    extraction dir when frozen (the sidecar's --add-data mirrors the repo layout,
    so _MEIPASS is a faux repo root), and the working checkout otherwise.
    """
    import os
    import sys

    if override := os.environ.get("BACKGLASS_RESOURCES"):
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", "."))
    return Path(__file__).resolve().parent.parent


REPO_ROOT = _repo_root()


def _csv(value: str | list[str] | None) -> list[str]:
    """Parse a comma-separated env value into a lowercased, stripped list."""
    if value is None:
        return []
    items = value if isinstance(value, list) else value.split(",")
    return [item.strip().lower() for item in items if item.strip()]


# ── routines ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Routine:
    """One recurring daily anchor: breakfast, gym, shower — life, as a fixed span."""

    name: str
    start_minute: int  # minute of the local day
    minutes: int


class RoutineError(ValueError):
    pass


_ROUTINE_RE = re.compile(r"^(?P<name>[^@,]+)@(?P<hh>\d{2}):(?P<mm>\d{2})\+(?P<dur>\d+)$")


def parse_routines(raw: str) -> list[Routine]:
    """`name@HH:MM+MINUTES,...` → routines sorted by start.

    One exception type for every malformed shape — the regex admits only digits, so
    the int() calls below cannot raise their own. Lives here rather than in
    plan/capacity so the field validator can call it without a circular import.
    """
    out: list[Routine] = []
    for part in (p.strip() for p in raw.split(",") if p.strip()):
        match = _ROUTINE_RE.match(part)
        if match is None:
            raise RoutineError(
                f"malformed routine {part!r}; expected name@HH:MM+MINUTES"
            )
        hour, minute = int(match["hh"]), int(match["mm"])
        duration = int(match["dur"])
        if hour > 23 or minute > 59:
            raise RoutineError(f"routine {part!r} has no such time of day")
        if not 0 < duration <= 24 * 60:
            raise RoutineError(f"routine {part!r} needs a duration of 1..1440 minutes")
        out.append(
            Routine(
                name=match["name"].strip(),
                start_minute=hour * 60 + minute,
                minutes=duration,
            )
        )
    return sorted(out, key=lambda r: r.start_minute)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ── identity ──────────────────────────────────────────────────────────
    owner_name: str = ""
    # Plural. docs/07 and .env.example say OWNER_EMAIL singular, but the owner has two
    # addresses and direction (i_owe vs owed_to_me) is decided against all of them.
    # See tasks/todo.md §Deviations #3.
    owner_emails: Annotated[list[str], NoDecode] = Field(default_factory=list)
    brief_to: str = ""

    # ── schedule ──────────────────────────────────────────────────────────
    default_tz: str = "America/Phoenix"
    alt_tz: str = "Asia/Kolkata"
    working_window: str = "09:00-18:00"
    #: The window on Saturday and Sunday, for owners who plan their weekends. Blank means
    #: "same as `working_window`" rather than "none" — a day with no window is expressed
    #: by leaving it out of `working_days`, and having two ways to say that is how one of
    #: them ends up wrong. It exists because a weekend that is plannable at all should not
    #: have to be plannable on weekday hours: the alternative is a Saturday that either
    #: does not exist or claims twelve hours of it.
    weekend_window: str = ""
    peak_window: str = "09:00-12:00"
    week_start: str = "monday"
    daily_reserve_minutes: int = 45
    brief_at: str = "06:00"
    #: docs/04 §1.2: the working window is "configured per weekday". Days absent here have
    #: no working window at all, which is how a weekend gets zero capacity rather than a
    #: plan nobody asked for.
    working_days: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"]
    )
    #: The other things in life, as fixed events on every day: `name@HH:MM+MINUTES`,
    #: comma-separated. They render on the schedule and the planner plans around
    #: them; only the ones inside the working window spend capacity (lunch does,
    #: breakfast does not). Empty string means none. Parsed and validated by
    #: `parse_routines` above — the same function capacity consumes it through, so a
    #: malformed entry fails at startup, not at 05:45.
    routines: str = (
        "breakfast@07:30+30,lunch@12:30+45,gym@17:30+60,shower@18:35+25,dinner@19:15+45"
    )

    # ── planner (docs/04 §1) ──────────────────────────────────────────────
    #: P6/P7. "At least one contiguous block of >= 90 minutes is protected per weekday
    #: when capacity allows", placed in the peak window.
    protected_block_minutes: int = 90
    #: P4. "Blocks have a minimum size of 25 minutes. Anything smaller is batched into a
    #: single 'small items' block."
    min_block_minutes: int = 25
    #: P3. "If capacity is under 60 minutes, do not propose a plan."
    min_capacity_minutes: int = 60
    #: docs/04 §1.2 buffer rule: 10 min after any meeting >= 30 min, 5 otherwise.
    buffer_long_minutes: int = 10
    buffer_short_minutes: int = 5
    buffer_long_threshold_minutes: int = 30
    #: P11. "An item that rolls over three times is flagged."
    rollover_question_at: int = 3
    #: A commitment this many days past due stops crowding the board's Overdue lane and
    #: folds into Stale — a backfilled mail window reaches months back, so obligations
    #: answered long ago arrive looking open, and thirty of them drown the three that
    #: are genuinely late. Folded, never hidden: same rows, same actions, one click away.
    stale_after_days: int = 14

    #: docs/04 §1.3. "Otherwise the default is by commitment type, configurable: review 30,
    #: draft 60, decision 15, meeting-prep 30, unknown 45." Never auto-adjusted — §1.3
    #: says report the estimate/actual ratio in the weekly review and let the owner change
    #: these, because a planner that silently retunes itself cannot be reasoned about.
    estimate_defaults: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "review:30",
            "draft:60",
            "decision:15",
            "meeting_prep:30",
            "unknown:45",
        ]
    )

    # ── goals (docs/04 §2.5) ──────────────────────────────────────────────
    #: G11: staleness and risk are computed independently and never merged into one
    #: "health" score. These thresholds belong to staleness only.
    stale_warn_days: int = 7
    stale_serious_days: int = 14
    #: G4: "A target that has been missed three weeks running is flagged as unrealistic."
    unrealistic_after_weeks: int = 3
    #: G7: "When under capacity by more than 25%, say that too. Slack is information."
    slack_report_fraction: float = 0.25
    #: Risk is required-rate versus observed-rate over the trailing four weeks (§2.5).
    risk_window_weeks: int = 4

    # ── people (Phase 6) ──────────────────────────────────────────────────
    #: Follow-up nudge thresholds. Applied only to curated profiles (role, org, or tags
    #: set) — every one-off correspondent going "cold" would be noise, not signal.
    people_touch_warn_days: int = 30
    people_touch_cold_days: int = 60

    # ── roadmap interview (Phase 6) ───────────────────────────────────────
    model_interview: str = "sonnet"
    interview_call_budget_usd: float = 0.25
    #: Hard cap on model calls per `roadmap start`, schema retries included.
    interview_max_calls: int = 3

    #: P14. "Active timezone comes from an explicit setting with an optional date range,
    #: not from IP geolocation, which is wrong exactly when travelling." Entries are
    #: `YYYY-MM-DD..YYYY-MM-DD:Area/Zone`; the end date is inclusive and may be omitted
    #: for an open-ended stay. Later entries win on overlap.
    tz_ranges: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ── brief delivery (docs/05, docs/10 §Email delivery) ─────────────────
    # A transactional provider, never the Gmail API. docs/10: keeping send capability out
    # of the Gmail credential makes "this system cannot email anyone as me" true by
    # construction rather than by discipline.
    brief_from: str = ""
    resend_api_key: str = ""
    #: Where source links and the B7 tracking pixel point. The dashboard, once it exists.
    dashboard_base_url: str = "http://127.0.0.1:8765"

    # ── model + cost ──────────────────────────────────────────────────────
    model_backend: Literal["claude_cli", "deepinfra", "anthropic"] = "anthropic"
    model_triage: str = "haiku"
    model_extract: str = "sonnet"
    model_api_key: str = ""
    deepinfra_base_url: str = "https://api.deepinfra.com/v1/openai"

    #: Screen (triage) through Apple's model via a Shortcuts "Use Model" action set
    #: to Private Cloud Compute — mail/message content stays inside Apple's privacy
    #: boundary until an item survives the screen. Extraction always stays on the
    #: schema-enforcing backend. Falls back to it too if the shortcut is missing.
    apple_triage: bool = False
    apple_triage_shortcut: str = "Backglass Triage"

    #: Enforced only against spend that is actually billed — see
    #: extract.client.spend_is_imputed and sync.SpendCap. Raised from 2000 on 2026-08-03
    #: at the owner's instruction, alongside the mail backfill.
    monthly_spend_cap_cents: int = 5000
    # A single item may never cost more than this. Passed to the billed backends as a
    # pre-flight worst-case guard; the CLI backend no longer receives it as
    # --max-budget-usd, because that ceiling was enforced against an imputed price.
    per_call_budget_usd: float = 0.10
    confidence_threshold: float = 0.7
    # Post-processing step 5 in extract-commitments.md says "fuzzy match" without an
    # algorithm. Ruling: normalized token-set ratio via stdlib difflib at this
    # threshold. See tasks/todo.md §Deviations #6.
    dedup_threshold: float = 0.85
    # Model calls take ~5s each. Bounded so a large first-run backfill does not
    # serialize into hours, and does not open unbounded subprocesses either.
    max_concurrency: int = 6
    # docs/02 §Cost control: "per_item_ceiling — skip and park anything whose input
    # exceeds it." Characters rather than tokens, because the ceiling exists to stop one
    # pathological message from costing more than a day of normal intake, and character
    # count is a good enough proxy that needs no tokenizer.
    per_item_char_ceiling: int = 60_000

    # ── data boundary (docs/08) ───────────────────────────────────────────
    boundary_mode: Literal["exclude", "full_scope"] = "exclude"
    boundary_deny_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)
    boundary_deny_addresses: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ── connectors ────────────────────────────────────────────────────────
    google_client_id: str = ""
    google_client_secret: str = ""
    # docs/02 §Tier 1 triage lists "sender on a known-noise list" as a rule-layer kill.
    # Domains or full addresses; a domain entry covers its subdomains. Every entry here is
    # a model call never made, so this is the cheapest lever on the bill.
    noise_senders: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Sources whose items are consumed deterministically (goals/reviews, capacity) and
    # carry nothing extractable — anki/avorio review tallies. Rule-dropped in tier 0 so
    # they never reach the triage model. A bare name covers its labelled variants
    # ("calendar" covers "calendar:personal"). Calendar is not in the default because an
    # event description can carry a commitment.
    structured_sources: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["anki", "avorio"]
    )
    # Learned noise (extract/noise.py). Off by default: promotion converts a
    # statistical judgment into a permanent free drop, so the default keeps a human in
    # the loop (`backglass noise suggest` / `promote`). The evidence bar — N model
    # drops, zero keeps ever, no commitment ever — is strict enough that auto mode is
    # defensible for an owner who wants it.
    noise_auto_promote: bool = False
    noise_promote_after: int = 5
    # Template dedup (extract/templates.py): a recurring templated mail is rule-dropped
    # once this many prior siblings were all dropped and none was ever kept. Three paid
    # drops ≈ a month of a weekly statement — evidence before the free pass.
    template_drop_after: int = 3
    # Batched triage (triage-batch.md): with this many or more items pending, tier 1
    # packs them ~TRIAGE_BATCH_SIZE per call and pays the instruction tokens once.
    # Uncertain/missing verdicts escalate to the full per-item pass automatically.
    triage_batch_size: int = 12
    triage_batch_min: int = 4
    # Batches are packed by character budget (`triage_batch_size` × the excerpt ceiling),
    # so short items pack denser than long ones. This caps how many can ride in one call
    # regardless: a verdict list the model stops aligning to the ids is worse than paying
    # for an extra call.
    triage_batch_max_items: int = 60

    # Which mailboxes to ingest. Each becomes a `credential` row with
    # source = "gmail:<label>", because credential is UNIQUE(user_id, source) and two
    # accounts cannot both be "gmail". See tasks/todo.md §Deviations #2.
    gmail_accounts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    #: Same labelling rule as Gmail — `credential` is UNIQUE(user_id, source), so each
    #: account becomes `calendar:<label>` / `drive:<label>`.
    calendar_accounts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    drive_accounts: Annotated[list[str], NoDecode] = Field(default_factory=list)

    obsidian_vault_path: Path | None = None
    canvas_base_url: str = ""
    canvas_token: str = ""

    # ── Phase 7 sources ───────────────────────────────────────────────────
    #: GitHub personal access token (classic or fine-grained; needs repo+read:user).
    github_token: str = ""
    #: Slack user token (xoxp-) plus the explicit conversations to read. Deliberately
    #: not "every channel" — a personal tool reads the handful of threads the owner
    #: names, and enumerating a workspace is how scope creep starts.
    slack_token: str = ""
    slack_channels: Annotated[list[str], NoDecode] = Field(default_factory=list)
    #: macOS Messages store. Empty disables. Requires Full Disk Access for the
    #: process running the sync.
    imessage_db_path: Path | None = None
    #: Instagram DMs (docs/07 §Instagram). The allowlist names the *only* group-chat
    #: titles and people either lane reads — same deliberate narrowness as Slack.
    #: Export lane: an unzipped Meta "Download Your Information" folder.
    #: The only iMessage conversations read. Group chats by display name, one-to-one
    #: threads by the other party's handle (phone number or address). Empty means the
    #: connector reports unhealthy rather than reading the whole store — see
    #: connectors/allowlist.py for why that is the default.
    imessage_chats: Annotated[list[str], NoDecode] = Field(default_factory=list)
    #: How far back an iMessage scan reaches. The cursor stops re-reading; this stops the
    #: first run reaching over an entire archive.
    imessage_lookback_days: int = 90
    instagram_export_path: Path | None = None
    instagram_chats: Annotated[list[str], NoDecode] = Field(default_factory=list)
    #: Live lane (experimental, ToS-violating, ban risk — see docs/07): both must be
    #: set to enable. The session file is created once via instagrapi dump_settings.
    instagram_username: str = ""
    instagram_session_file: Path | None = None
    #: A drop folder: every file in it becomes a source item.
    inbox_folder_path: Path | None = None
    #: Apple-native sources via the OS automation bridge (JXA/osascript). Boolean
    #: opt-ins because there is nothing else to configure — permission lives in
    #: System Settings → Automation, not in an env var.
    apple_notes: bool = False
    apple_reminders: bool = False
    #: Read Contacts.app through the same bridge, to answer "who is +14802411748". Not
    #: an ingest source: it writes identifiers onto `entity` rows and no source_item —
    #: see backglass/contacts.py.
    apple_contacts: bool = False
    #: Read Calendar.app through the automation bridge. Reaches whatever accounts macOS
    #: already syncs, so it needs no Google OAuth client and no Full Disk Access — see
    #: connectors/apple_calendar.py for why that is the point.
    apple_calendar: bool = False
    #: Calendars to leave out, comma-separated. Subscribed holiday and birthday feeds
    #: would otherwise consume the day planner's capacity every week.
    apple_calendar_skip: Annotated[list[str], NoDecode] = Field(default_factory=list)
    #: Mail.app's local store, normally ~/Library/Mail. Empty disables. A path rather
    #: than a boolean because the version directory moves with macOS (V9, V10, …) and
    #: because an archived copy of a mail store is a legitimate thing to point at.
    #: Needs Full Disk Access, like the Messages store — see connectors/apple_mail.py.
    apple_mail_path: Path | None = None
    #: Mailboxes whose mail must never enter the ledger, named by the address they are
    #: delivered to. The other half of docs/08's 2026-08-03 decision: the denylist is
    #: empty because the out-of-scope inbox is not connected, and this is what keeps that
    #: true if it ever is. Enforced in the connector, before persistence (D1), so adding
    #: the account to Mail.app cannot quietly start ingesting it.
    boundary_out_of_scope_accounts: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    #: How far back a first mail scan reaches. Longer than the message default: mail
    #: carries deadlines with more notice than a group chat does.
    apple_mail_lookback_days: int = 120

    # ── Spaced-repetition sources ─────────────────────────────────────────
    #: Paths to the review apps' own local SQLite stores; empty disables. Opened
    #: read-only+immutable like the Messages store. Only review counts and due
    #: loads are ingested, never card content, so the docs/08 boundary has no
    #: surface here — there are no addresses in a review tally.
    anki_db_path: Path | None = None
    avorio_db_path: Path | None = None
    #: The cadence target that review-day checkpoints land on. Unset means the
    #: connectors still ingest (brief and capacity keep working) but no
    #: checkpoints are written — binding progress to a target is an owner choice,
    #: not a default.
    reviews_target_id: int | None = None

    # ── heartbeat (backglass/heartbeat.py) ────────────────────────────────
    #: How old the newest completed run may get before the dashboard and the brief say
    #: the ledger is stale. Four times the 30-minute sync cadence: one missed run is
    #: weather, four in a row is a dead launchd job. docs/11 §8.
    sync_stale_after_hours: float = 2.0

    # ── storage ───────────────────────────────────────────────────────────
    db_path: Path = Path("./data/backglass.db")
    #: Where `backglass backup` writes snapshots. Outside the checkout on purpose: a
    #: backup inside `data/` dies with the same `rm -rf` as the original, and one inside
    #: the repo is bait for the cloud-sync exclusion docs/08 §Storage relies on.
    backup_dir: Path = Field(
        default_factory=lambda: Path.home()
        / "Library"
        / "Application Support"
        / "Backglass"
        / "backups"
    )

    # NoDecode above is load-bearing: pydantic-settings JSON-decodes complex types from
    # the environment before validators run, so a comma-separated OWNER_EMAILS raises a
    # SettingsError rather than reaching this. Constructing Settings(...) in a test hides
    # that entirely, which is why tests/test_config.py reads from the environment instead.
    @field_validator(
        "owner_emails",
        "boundary_deny_domains",
        "boundary_deny_addresses",
        "gmail_accounts",
        "noise_senders",
        "structured_sources",
        "working_days",
        "calendar_accounts",
        "drive_accounts",
        "estimate_defaults",
        "tz_ranges",
        "slack_channels",
        "instagram_chats",
        "imessage_chats",
        "apple_calendar_skip",
        "boundary_out_of_scope_accounts",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value: object) -> list[str]:
        if isinstance(value, str | list) or value is None:
            return _csv(value)
        raise TypeError(f"expected a comma-separated string or list, got {type(value)}")

    @field_validator("routines")
    @classmethod
    def _check_routines(cls, value: str) -> str:
        parse_routines(value)  # fail at startup, not at 05:45
        return value

    @field_validator("working_window", "weekend_window", "peak_window")
    @classmethod
    def _check_window(cls, value: str) -> str:
        """Fail at startup, not at 05:45 — same reason as `routines` above.

        A blank `weekend_window` is the documented "same as the weekday one", so it is
        the one value that skips the check rather than raising on an empty string.
        """
        if value:
            from backglass.plan.timezones import parse_window

            parse_window(value)
        return value

    # .env.example ships these four blank ("APPLE_TRIAGE=", "REVIEWS_TARGET_ID=") so a
    # fresh `cp .env.example .env` cloner sees the exact key to fill in. Pydantic
    # coerces a present-but-empty env string toward each field's real type before
    # falling back to a default, and "" is neither a valid bool nor a valid int — so
    # left blank, `Settings()` raised a bare ValidationError/traceback instead of using
    # the documented off/unset default. Only these four fields are bool/int-typed *and*
    # shipped blank in the template; every other int/float field in .env.example carries
    # a real numeric default, so this is not needed there.
    @field_validator(
        "apple_triage",
        "apple_notes",
        "apple_reminders",
        "apple_calendar",
        "apple_contacts",
        mode="before",
    )
    @classmethod
    def _blank_bool_is_false(cls, value: object) -> object:
        return False if value == "" else value

    @field_validator("reviews_target_id", mode="before")
    @classmethod
    def _blank_int_is_none(cls, value: object) -> object:
        return None if value == "" else value

    def owns(self, address: str | None) -> bool:
        """True if this address is one of the owner's own."""
        if not address:
            return False
        return _extract_email(address) in self.owner_emails


def _extract_email(raw: str) -> str:
    """Pull the bare address out of `Name <addr@host>` or a plain address."""
    raw = raw.strip().lower()
    if "<" in raw and ">" in raw:
        raw = raw[raw.rindex("<") + 1 : raw.rindex(">")]
    return raw.strip()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
