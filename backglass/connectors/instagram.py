"""Instagram DMs. docs/07 §Instagram — two lanes, one allowlist.

Instagram has no official API for a personal account's DMs (the Basic Display API died
December 2024; the Graph API reads only professional-account inboxes). So this module
ships two connectors, both deliberately narrow in the Slack sense: they read only the
group chats and people the owner names, never the whole inbox.

**Export lane** (`InstagramExportConnector`, source `instagram`). The owner periodically
requests Meta's "Download Your Information" export and points
`INSTAGRAM_EXPORT_PATH` at the unzipped folder. Official, zero ban risk, no credential —
a local source in the Obsidian/iMessage shape. The data is only as fresh as the last
export, which is the accepted trade-off of the safe lane.

  * Threads live at `your_instagram_activity/messages/inbox/<thread>/message_N.json`
    (older exports: `messages/inbox/...`). Both layouts are searched.
  * Every string in the export is UTF-8 that Meta serialised as latin-1 escapes, so
    "café 🎂" arrives as "cafÃ© ð\x9f\x8e\x82". `_fix_mojibake` reverses that exact
    round-trip and leaves already-clean strings alone.
  * `timestamp_ms` is milliseconds since the Unix epoch. The cursor is the highest one
    seen in an allowed thread, so a fresh export dropped on the same folder yields only
    what is newer — and `content_hash` makes any overlap a no-op write anyway (docs/03).
  * System strings ("Liked a message", "X sent an attachment.", reactions) are skipped:
    they read like messages and are not, the tapback problem wearing a new hat.

**Live lane** (`InstagramLiveConnector`, source `instagram:live`). instagrapi speaking
the private mobile API. Fresh every run, but it violates Instagram ToS and automated
logins can checkpoint or ban the account — which is why it is flag-gated, marked
experimental, and the export lane exists. The session file is created once by the owner
(instagrapi `dump_settings`) so routine runs never perform a fresh login, the single
biggest ban-risk reducer. The instagrapi import is lazy and the client injectable, so
tests exercise the lane with a fake and no test ever calls the live API.

**Allowlist.** `INSTAGRAM_CHATS` holds group-chat titles and usernames/display names,
matched case-insensitively after mojibake repair. A group thread is allowed by title; a
one-to-one thread by either participant's name. Everything else produces no source_item
row and no model call — counted per run under the `allowlist` rule, same shape as the
docs/08 boundary tally.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backglass.chats import Sighting
from backglass.connectors.allowlist import Allowlist, normalise
from backglass.connectors.base import Cursor, Health, SourceItem, content_hash
from backglass.connectors.boundary import Boundary

#: Where Meta puts message threads, newest export layout first.
_INBOX_ROOTS = ("your_instagram_activity/messages/inbox", "messages/inbox")

#: Export strings that read like messages and are not. Anchored so a real sentence that
#: merely contains one of these is kept — triage.md: a false negative loses a commitment
#: permanently, a false positive costs one model call.
_SYSTEM_CONTENT = re.compile(
    r"^(liked a message$"
    r"|.{0,80} sent an attachment\.$"
    r"|reacted .{1,40} to your message\s*$"
    r"|you sent an attachment\.$)",
    re.IGNORECASE,
)

_ALLOWLIST_RULE = "allowlist"


def _fix_mojibake(text: str) -> str:
    """Reverse Meta's UTF-8-as-latin-1 serialisation; leave clean strings alone."""
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


#: Re-exported: `Allowlist` moved to connectors/allowlist.py when iMessage needed the same
#: rule, and the callers that import it from here keep working.
_normalise = normalise


@dataclass(kw_only=True)
class InstagramExportConnector:
    """A Meta "Download Your Information" folder. One message in, one SourceItem out."""

    export_path: Path
    allowlist: Allowlist
    boundary: Boundary

    #: Only final once `fetch()` has been exhausted.
    cursor: Cursor = None
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)
    #: Every thread in the export, allowed or not — filled by `discover()` at the top of
    #: `fetch()`, independent of the cursor, for the same reason iMessage's is: the page
    #: that fills the allowlist is fed from these, so tying them to the watermark would
    #: mean nothing can ever be chosen once the cursor is current.
    seen_chats: dict[str, Sighting] = field(default_factory=dict)
    #: Discovery rescans the whole export, so these are totals, not increments.
    sightings_are_cumulative: bool = False

    #: Both lanes read the same conversations, so their sightings and decisions live
    #: under one source in `monitored_chat` — a chat monitored on the page must be
    #: monitored however it arrives.
    chats_source = "instagram"

    @property
    def name(self) -> str:
        return "instagram"

    def health(self) -> Health:
        if not self.export_path.exists():
            return Health(
                name=self.name,
                ok=False,
                detail=(
                    f"export folder not found at {self.export_path} — request a JSON "
                    "export at accountscenter.instagram.com → Your information and "
                    "permissions → Download your information, unzip it, and point "
                    "INSTAGRAM_EXPORT_PATH at the folder"
                ),
            )
        if not self.allowlist:
            return Health(
                name=self.name,
                ok=False,
                detail=(
                    "no conversations are monitored yet — open /chats to choose. The "
                    "sync still discovers conversations while none are chosen, so the "
                    "list fills itself in."
                ),
            )
        if self._inbox_dir() is None:
            return Health(
                name=self.name,
                ok=False,
                detail=(
                    f"no messages/inbox under {self.export_path} — the export must be "
                    "requested in JSON format (not HTML) and include Messages"
                ),
            )
        return Health(name=self.name, ok=True)

    def fetch(self, since: Cursor) -> Iterator[SourceItem]:
        """Yield allowed messages with a `timestamp_ms` above the cursor.

        `since=None` is a full scan of the export — the first run, or a fresh export
        after cursor loss, and content_hash makes the overlap free either way.
        """
        self.excluded = 0
        self.excluded_by_rule = {}
        self.seen_chats = self.discover()
        watermark = _parse(since)
        highest = watermark

        inbox = self._inbox_dir()
        if inbox is None:
            raise FileNotFoundError(f"no messages/inbox under {self.export_path}")

        for thread_dir in sorted(p for p in inbox.iterdir() if p.is_dir()):
            thread = _read_thread(thread_dir)
            if thread is None:
                continue
            fresh = [m for m in thread.messages if m.timestamp_ms > watermark]
            if not fresh:
                continue
            # Advance past excluded threads too, or a chatty non-allowed thread would
            # be re-read and re-counted on every run forever.
            highest = max(highest, *(m.timestamp_ms for m in fresh))
            if not self.allowlist.allows(
                title=thread.title, participants=thread.participants
            ):
                self._exclude(_ALLOWLIST_RULE, len(fresh))
                continue
            for message in fresh:
                item = self._to_item(thread, message)
                if item is not None:
                    yield item

        self.cursor = str(highest)

    def discover(self) -> dict[str, Sighting]:
        """Every thread in the export, with how much it said — cursor be damned.

        The key is what the allowlist can match later: the thread title (Meta writes the
        counterparty's name there for a one-to-one), falling back to the first
        participant. A key `Allowlist` cannot match would let the page turn on a chat
        the connector then fails to recognise.
        """
        inbox = self._inbox_dir()
        if inbox is None:
            return {}
        seen: dict[str, Sighting] = {}
        for thread_dir in sorted(p for p in inbox.iterdir() if p.is_dir()):
            thread = _read_thread(thread_dir)
            if thread is None:
                continue
            key = thread.title or next(iter(thread.participants), None)
            if not key:
                continue
            seen[key] = Sighting(
                key=key,
                display_name=key,
                # Meta lists the owner among the participants, so two names is a DM.
                kind="group" if len(thread.participants) > 2 else "dm",
                participants=len(thread.participants) or None,
                messages=len(thread.messages),
            )
        return seen

    def _inbox_dir(self) -> Path | None:
        for root in _INBOX_ROOTS:
            candidate = self.export_path / root
            if candidate.is_dir():
                return candidate
        return None

    def _exclude(self, rule: str, count: int = 1) -> None:
        self.excluded += count
        self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + count

    def _to_item(self, thread: _Thread, message: _Message) -> SourceItem | None:
        text = message.content
        if not text or _SYSTEM_CONTENT.match(text):
            return None

        # D1/D4, same seam as every other connector: checked before persistence,
        # before hashing, before any model call.
        verdict = self.boundary.check([message.sender])
        if not verdict.allowed:
            self._exclude(verdict.matched_rule or "?")
            return None

        occurred_at = (
            datetime.fromtimestamp(message.timestamp_ms / 1000, tz=UTC)
            .replace(microsecond=0)
            .isoformat()
        )
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        return SourceItem(
            source=self.name,
            # Stable across re-exports: the thread directory name persists, and the
            # content digest disambiguates two messages in the same millisecond.
            external_id=f"{thread.key}:{message.timestamp_ms}:{digest}",
            occurred_at=occurred_at,
            author=message.sender,
            title=thread.title or ", ".join(thread.participants),
            body_text=text,
            raw_json=json.dumps(
                {"chat": thread.title, "thread": thread.key, "sender": message.sender},
                sort_keys=True,
            ),
            content_hash=content_hash(
                author=message.sender,
                title=thread.title,
                body_text=text,
                occurred_at=occurred_at,
            ),
        )


@dataclass(frozen=True)
class _Message:
    sender: str
    timestamp_ms: int
    content: str


@dataclass(frozen=True)
class _Thread:
    key: str
    title: str | None
    participants: list[str]
    messages: list[_Message]


def _read_thread(thread_dir: Path) -> _Thread | None:
    """Every `message_N.json` in one thread directory, mojibake repaired."""
    files = sorted(thread_dir.glob("message_*.json"))
    if not files:
        return None

    title: str | None = None
    participants: list[str] = []
    messages: list[_Message] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw_title = _fix_mojibake(str(data.get("title") or "")).strip()
        # Meta uses the counterparty's name as the "title" of a one-to-one thread; a
        # real group title is whatever the owner typed. Both are useful, so keep it.
        title = title or (raw_title or None)
        participants = [
            _fix_mojibake(str(p.get("name") or "")).strip()
            for p in data.get("participants") or []
            if str(p.get("name") or "").strip()
        ] or participants
        for raw in data.get("messages") or []:
            sender = _fix_mojibake(str(raw.get("sender_name") or "")).strip()
            content = _fix_mojibake(str(raw.get("content") or "")).strip()
            try:
                timestamp_ms = int(raw.get("timestamp_ms") or 0)
            except (TypeError, ValueError):
                timestamp_ms = 0
            if not sender or timestamp_ms <= 0:
                continue
            messages.append(
                _Message(sender=sender, timestamp_ms=timestamp_ms, content=content)
            )

    return _Thread(
        key=thread_dir.name, title=title, participants=participants, messages=messages
    )


@dataclass(kw_only=True)
class InstagramLiveConnector:
    """The private mobile API via instagrapi. Experimental, flag-gated, ban-risk lane.

    `client_factory` exists for tests: anything returning an object with
    `direct_threads(amount=)` and `direct_messages(thread_id, amount=)` in instagrapi's
    shapes. Production leaves it None and gets a real, session-loaded client — lazily,
    so the dependency is only imported when the owner has opted in.
    """

    username: str
    session_file: Path | None
    allowlist: Allowlist
    boundary: Boundary
    client_factory: Callable[[], Any] | None = None
    #: How far back each run looks. Small on purpose: gentle polling is the second
    #: biggest ban-risk reducer after never re-logging-in.
    threads_amount: int = 20
    messages_amount: int = 50
    #: The one deeper window `fetch` is allowed when the ordinary one does not reach the
    #: watermark, and the point past which it stops asking rather than crawl. See fetch().
    max_messages_amount: int = 500

    cursor: Cursor = None
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)
    #: Threads whose backlog was still deeper than `max_messages_amount` this run. Non-zero
    #: means the watermark deliberately did not move — see fetch().
    truncated_threads: int = 0
    #: The threads `direct_threads` listed this run, allowed or not. A window of the
    #: newest N, so each run's counts are increments over what earlier runs saw.
    seen_chats: dict[str, Sighting] = field(default_factory=dict)
    sightings_are_cumulative: bool = True

    #: Shared with the export lane — see InstagramExportConnector.chats_source.
    chats_source = "instagram"

    @property
    def name(self) -> str:
        return "instagram:live"

    def health(self) -> Health:
        if not self.allowlist:
            return Health(
                name=self.name,
                ok=False,
                detail=(
                    "no conversations are monitored yet — open /chats to choose. The "
                    "sync still discovers conversations while none are chosen, so the "
                    "list fills itself in."
                ),
            )
        if self.client_factory is not None:
            return Health(name=self.name, ok=True)
        try:
            import instagrapi  # noqa: F401
        except ImportError:
            return Health(
                name=self.name,
                ok=False,
                detail="instagrapi not installed — `uv add instagrapi` to enable the live lane",
            )
        if self.session_file is None or not self.session_file.exists():
            return Health(
                name=self.name,
                ok=False,
                detail=(
                    "no session file — create one once with instagrapi "
                    "(Client().login(...); Client().dump_settings(path)) and point "
                    "INSTAGRAM_SESSION_FILE at it; a stored session avoids the fresh "
                    "logins that get accounts checkpointed"
                ),
            )
        return Health(name=self.name, ok=True)

    def fetch(self, since: Cursor) -> Iterator[SourceItem]:
        """Allowed messages newer than the cursor, and a cursor that never outruns them.

        instagrapi hands back the newest `amount` messages of a thread, not one page of a
        walk, so a fixed window is a *view* of the backlog rather than the whole of it.
        Moving the watermark to the newest thing in a truncated view steps over every
        older message the window did not reach, and those are then unreachable forever:
        the next run asks for the newest N again and discards anything at or below the
        watermark. One missed run plus a busy group chat is the whole recipe.

        So the window is paginated towards the watermark rather than fixed (option (a),
        the shape the REST connectors already use): a window that comes back full and
        still ends above the watermark has not reached it, and that thread is re-read once
        at `max_messages_amount`. If even that does not reach it, the run refuses to
        advance the watermark at all — below a truncated window nothing is covered, so the
        oldest safely-covered point is where the run started. A re-read costs nothing
        because content_hash absorbs it (docs/03); a skipped message costs a commitment,
        and docs/07 says an unreachable source is the failure that matters here.

        The ceiling is not a compromise on that rule, it is the ban-risk budget: crawling
        the private mobile API to exhaustion is exactly the behaviour that gets an account
        checkpointed, which is the risk this whole lane is organised around. Correctness
        is bought with re-reads, never with an unbounded crawl.

        A first run has no cursor and therefore no gap to lose — there is no earlier
        coverage for the window to be contiguous with — so the fixed window stands as the
        deliberate starting point rather than triggering a backfill of the whole inbox.
        """
        self.excluded = 0
        self.excluded_by_rule = {}
        self.truncated_threads = 0
        self.seen_chats = {}
        watermark = _parse_iso(since)
        highest = watermark

        client = self._client()
        for thread in client.direct_threads(amount=self.threads_amount):
            title = (getattr(thread, "thread_title", None) or "").strip() or None
            users = {
                str(u.pk): str(getattr(u, "username", "") or u.pk)
                for u in getattr(thread, "users", []) or []
            }
            # Sighted before the allowlist, like every other messaging lane: a thread
            # nobody has named must surface as a question, not vanish. The key falls
            # back to a single username because the allowlist matches participants
            # individually — a joined string would make the Monitor button a no-op.
            key = title or next(iter(sorted(users.values())), None)
            if key:
                self.seen_chats[key] = Sighting(
                    key=key,
                    display_name=key,
                    kind="group" if len(users) > 1 else "dm",
                    participants=len(users) or None,
                    messages=0,
                )
            if not self.allowlist.allows(
                title=title, participants=list(users.values())
            ):
                self._exclude(_ALLOWLIST_RULE)
                continue
            fresh, reached = self._messages_since(client, thread.id, watermark)
            if key:
                self.seen_chats[key] = Sighting(
                    key=key,
                    display_name=key,
                    kind="group" if len(users) > 1 else "dm",
                    participants=len(users) or None,
                    messages=len(fresh),
                )
            if not reached:
                self.truncated_threads += 1
            for moment, message in fresh:
                highest = moment if highest is None else max(highest, moment)
                text = (getattr(message, "text", None) or "").strip()
                if not text:
                    continue
                author = users.get(str(getattr(message, "user_id", "")), "") or str(
                    getattr(message, "user_id", "?")
                )
                verdict = self.boundary.check([author])
                if not verdict.allowed:
                    self._exclude(verdict.matched_rule or "?")
                    continue
                occurred_at = moment.replace(microsecond=0).isoformat()
                item_title = title or ", ".join(sorted(users.values()))
                yield SourceItem(
                    source=self.name,
                    external_id=f"{thread.id}:{message.id}",
                    occurred_at=occurred_at,
                    author=author,
                    title=item_title,
                    body_text=text,
                    raw_json=json.dumps(
                        {"chat": title, "thread": str(thread.id), "sender": author},
                        sort_keys=True,
                    ),
                    content_hash=content_hash(
                        author=author,
                        title=item_title,
                        body_text=text,
                        occurred_at=occurred_at,
                    ),
                )

        # Leaving `cursor` None leaves the stored one alone (sync.py `_ingest` only writes
        # a cursor it was given), which is precisely "hold at the last point we can prove
        # we covered".
        if highest is not None and not self.truncated_threads:
            self.cursor = highest.isoformat()

    def _messages_since(
        self, client: Any, thread_id: Any, watermark: datetime | None
    ) -> tuple[list[tuple[datetime, Any]], bool]:
        """The messages newer than `watermark`, and whether the window provably reached it.

        Reached means one of three things: there is no watermark to reach, the thread
        returned fewer messages than were asked for and is therefore exhausted, or the
        window contains something at or below the watermark and so overlaps the covered
        range. Anything else is a gap of unknown width, and the caller treats it as one.
        """
        amount = self.messages_amount
        while True:
            window = list(client.direct_messages(thread_id, amount=amount))
            moments = [
                (moment, message)
                for message, moment in (
                    (m, _as_utc(getattr(m, "timestamp", None))) for m in window
                )
                if moment is not None
            ]
            reached = (
                watermark is None
                or len(window) < amount
                or any(moment <= watermark for moment, _ in moments)
            )
            if reached or amount >= self.max_messages_amount:
                fresh = [
                    (moment, message)
                    for moment, message in moments
                    if watermark is None or moment > watermark
                ]
                return fresh, reached
            amount = self.max_messages_amount

    def _client(self) -> Any:
        if self.client_factory is not None:
            return self.client_factory()
        from instagrapi import Client

        client = Client()
        if self.session_file is None or not self.session_file.exists():
            raise FileNotFoundError(
                f"instagram session file not found at {self.session_file}"
            )
        client.load_settings(self.session_file)
        return client

    def _exclude(self, rule: str, count: int = 1) -> None:
        self.excluded += count
        self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + count


def _parse(cursor: Cursor) -> int:
    """The export cursor is a stringified timestamp_ms. Unreadable means full scan."""
    if not cursor:
        return 0
    try:
        return int(str(cursor))
    except ValueError:
        return 0


def _parse_iso(cursor: Cursor) -> datetime | None:
    if not cursor:
        return None
    try:
        moment = datetime.fromisoformat(str(cursor))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
