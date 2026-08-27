"""Slack. Conversations the owner has chosen, read through the same allowlist gate as
iMessage and Instagram.

**Scope, and the 2026-08-24 ruling that changed it.** This connector used to read only the
raw channel IDs listed in `SLACK_CHANNELS`, and its docstring said it would never enumerate
conversations. That rule survived contact with exactly one problem: the owner has to *get*
a channel ID from somewhere, and Slack does not put one anywhere a person can see. The
no-crawl instinct behind the old rule was right and its mechanism was wrong — the answer
the rest of this codebase already uses is discovery plus consent, not blindness.

So: every run enumerates the conversations the owner is a member of (`users.conversations`)
and records each as a `Sighting` on `monitored_chat`. **Nothing is read until it is chosen
on /chats.** Enumeration is metadata — a name, a kind, a member count — and never message
text. That is the same contract `connectors/imessage.py` and `connectors/instagram.py`
carry, and the reason it is safe: consent is asked per conversation, and the default is no.

Discovery is deliberately *not* gated on the allowlist. tasks/lessons.md, 2026-08-03: a page
whose input comes from the thing the page disables stays blank forever, and an empty
allowlist is precisely the state in which discovery matters most.

`SLACK_CHANNELS` still works. `chats.seed_from_env` turns whatever is in `.env` into decided
`monitor` rows on the first sync, after which the table is the only thing that decides.

Wire details:

  - GET to `https://slack.com/api/<method>`, `Authorization: Bearer`.
  - `oldest=<watermark>`, `limit=200`, paginated via `response_metadata.next_cursor`.
  - Slack answers 200 with `{"ok": false, "error": "..."}` for auth and quota failures, so
    the envelope is checked on every call rather than trusting the HTTP status.

**Cursor.** A versioned JSON document, not a single watermark:

    {"v": 1, "floor": "<ts>", "channels": {"C0…": "<ts>"}, "threads": {"C0…:<parent>": "<ts>"}}

Every `ts` is Slack's fractional-seconds string kept at full precision. It is never
parsed-and-reformatted for storage, only for comparison — truncating it is the Obsidian
watermark bug in tasks/lessons.md wearing a different hat.

The old shape was one `ts` for the whole workspace, and it was a real defect rather than a
simplification. The watermark only advanced after a fully clean pass over every channel
*and* every thread fan-out, and a rate-limit stop reset it to where the run began. Slack
grants new non-Marketplace apps roughly one `conversations.history` request per minute
(tasks/todo-archive-2026-08-01.md:495), so a first backfill cannot finish a clean pass — the
watermark pinned at zero forever and every run re-read the workspace from the start. Per
channel, progress is durable: a channel that finished advances even when the next one is
throttled mid-run. A bare `ts` string left over from the old shape is read as a **floor**
under every channel, which is the only migration a cursor gets.

**Threads.** `conversations.history` returns thread *parents* only (docs/12 §4), so a promise
made inside a thread reply — the usual place a promise is made — is invisible to a
history-only reader. Two passes cover it:

  1. Every parent seen with `reply_count > 0` gets a `conversations.replies` fan-out and is
     registered in the cursor's `threads` map.
  2. Registered threads are re-polled on later runs from their own watermark, whether or not
     the parent still falls inside the channel window.

Pass 2 is the fix for what this module previously documented as an accepted limitation: a
reply landing today on a thread started before the cursor does not move the parent's `ts`,
Slack does not re-serve the parent into `conversations.history` because `latest_reply`
changed, and so that reply was never read. The map is bounded by `thread_window_days` —
threads with no activity inside the window are dropped — and by `_THREAD_REPOLL_LIMIT` per
run, oldest watermark first, so a large map costs a bounded number of requests and every
thread still gets its turn. The deferred count is reported on `threads_deferred` rather than
being silently capped.

**Names, and why they are not hashed.** `title` and `author` carry resolved names
(`#founders`, `Dana Ruiz`) because rule 1's provenance line is read by a person, and
`#C0ABCDEF` is not provenance. But a display name is *not* deterministic given the item's
id: a rename upstream, or a `users.list` call that fails once, would recompute a different
`content_hash` for a message already in the ledger, and the 0002 trigger turns that into a
failed-looking sync (tasks/lessons.md, 2026-07-30 — and `canvas:ics` is living it right now).

So the hash is computed over the **stable identity** — the raw channel ID and the raw user
ID — while the fields carry the readable form. Change detection keys on what cannot drift;
the display keys on what a person can read. A rename therefore does not rewrite history,
which is also the correct behaviour for an immutable capture.

**Subtypes.** The filter is a blocklist of machine chatter, not a blanket "has a subtype".
The blanket version dropped `thread_broadcast` — a genuine human reply, also sent to the
channel — and `file_share`, which carries the owner's own message text alongside the file.
Both are exactly where a commitment shows up.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from backglass.chats import Sighting
from backglass.connectors.allowlist import Allowlist
from backglass.connectors.base import Cursor, Health, SourceItem, content_hash
from backglass.connectors.boundary import Boundary

API = "https://slack.com/api"

#: docs/07's serialize-and-back-off rule. Slack's own guidance is the same: one request at
#: a time, obey the retry delay.
BACKOFF_SECONDS = (2, 8, 30)

#: `conversations.history` caps at 1000; 200 is Slack's recommended page size.
PAGE_LIMIT = "200"

#: What `users.conversations` enumerates. Everything the owner is a member of, including
#: DMs — a DM is where most of a personal ledger's commitments are made.
CONVERSATION_TYPES = "public_channel,private_channel,im,mpim"

#: How many pre-existing threads may be re-polled in one run. Each costs one request, and
#: the tightest tier grants about one per minute. Threads are taken oldest-watermark-first
#: so the queue drains fairly; the remainder is reported, never silently dropped.
_THREAD_REPOLL_LIMIT = 25

#: Machine chatter. Anything else — including an unknown future subtype — is treated as a
#: person talking, because dropping a human message is the expensive direction of this
#: mistake and an empty `text` is filtered anyway.
NOISE_SUBTYPES = frozenset(
    {
        "bot_add",
        "bot_message",
        "bot_remove",
        "channel_archive",
        "channel_convert_to_private",
        "channel_convert_to_public",
        "channel_join",
        "channel_leave",
        "channel_name",
        "channel_purpose",
        "channel_topic",
        "channel_unarchive",
        "ekm_access_denied",
        "group_archive",
        "group_join",
        "group_leave",
        "group_name",
        "group_purpose",
        "group_topic",
        "group_unarchive",
        "huddle_thread",
        "message_changed",
        "message_deleted",
        "message_replied",
        "pinned_item",
        "reminder_add",
        "sh_room_created",
        "tombstone",
        "unpinned_item",
    }
)

#: Same pattern the Obsidian connector matches on, for the same reason (docs/08 D1).
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

#: A JSON payload, or a transport double in tests. Takes (path, params).
Transport = Callable[[str, dict[str, str]], dict[str, Any]]


class SlackError(RuntimeError):
    """Slack answered `{"ok": false}`. Carries the error string, which is the useful part."""

    def __init__(self, error: str):
        super().__init__(error)
        self.error = error


class SlackRateLimited(SlackError):
    """Quota exhausted. Not a failure — a reason to stop early and keep what was read."""


# ── the cursor ────────────────────────────────────────────────────────────


@dataclass
class Watermarks:
    """Per-channel and per-thread positions, serialized into the credential row's cursor.

    `floor` is the legacy single watermark: a bare `ts` string from the old cursor shape
    applies under every channel that has no entry of its own, so an existing install does
    not re-read its whole history the first time this version runs.
    """

    floor: str = ""
    channels: dict[str, str] = field(default_factory=dict)
    threads: dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, cursor: Cursor) -> Watermarks:
        raw = str(cursor or "").strip()
        if not raw:
            return cls()
        if not raw.startswith("{"):
            # The old shape: one `ts` for the whole workspace.
            return cls(floor=raw)
        try:
            doc = json.loads(raw)
        except ValueError:
            # A cursor nobody can read is a cursor at the beginning. Re-reading costs
            # requests and, because of content_hash, zero writes.
            return cls()
        if not isinstance(doc, dict):
            return cls()
        return cls(
            floor=str(doc.get("floor") or ""),
            channels=_ts_map(doc.get("channels")),
            threads=_ts_map(doc.get("threads")),
        )

    def dumps(self) -> str:
        doc: dict[str, Any] = {
            "v": 1,
            "channels": dict(sorted(self.channels.items())),
            "threads": dict(sorted(self.threads.items())),
        }
        if self.floor:
            doc["floor"] = self.floor
        return json.dumps(doc, sort_keys=True, separators=(",", ":"))

    def for_channel(self, channel_id: str) -> str:
        return _later(self.channels.get(channel_id, ""), self.floor)

    def for_thread(self, channel_id: str, parent_ts: str) -> str:
        """A thread's own position, or the channel's if it has never been polled.

        Deliberately *not* raised to the channel watermark once it exists: a thread
        watermark below the channel's is the whole point — it is what lets a late reply on
        an old parent be found.
        """
        stored = self.threads.get(_thread_key(channel_id, parent_ts), "")
        return stored or self.for_channel(channel_id)

    def advance_channel(self, channel_id: str, ts: str) -> None:
        if ts and _newer(ts, self.channels.get(channel_id, "")):
            self.channels[channel_id] = ts

    def advance_thread(self, channel_id: str, parent_ts: str, ts: str) -> None:
        key = _thread_key(channel_id, parent_ts)
        if ts and _newer(ts, self.threads.get(key, "")):
            self.threads[key] = ts

    def register_thread(self, channel_id: str, parent_ts: str, floor: str) -> None:
        """Remember a thread exists, so later runs re-poll it from here."""
        self.threads.setdefault(_thread_key(channel_id, parent_ts), floor or parent_ts)

    def prune_threads(self, *, before: float, keep: Sequence[str] = ()) -> int:
        """Forget threads with no activity inside the window. Returns how many went.

        Bounds the map. A thread that has been quiet for a month is not where today's
        commitment is being made, and keeping it costs a request per run forever.
        """
        kept = set(keep)
        stale: list[str] = []
        for key, ts in self.threads.items():
            if key in kept:
                continue
            seconds = _seconds(ts)
            if seconds is not None and seconds < before:
                stale.append(key)
        for key in stale:
            del self.threads[key]
        return len(stale)

    def channels_of_threads(self) -> dict[str, list[tuple[str, str]]]:
        """`{channel_id: [(parent_ts, watermark), …]}`, for the re-poll pass."""
        out: dict[str, list[tuple[str, str]]] = {}
        for key, ts in self.threads.items():
            channel_id, _, parent_ts = key.partition(":")
            if channel_id and parent_ts:
                out.setdefault(channel_id, []).append((parent_ts, ts))
        return out


@dataclass(kw_only=True)
class SlackConnector:
    token: str
    boundary: Boundary
    #: What the owner has chosen on /chats, unioned with `SLACK_CHANNELS` — see
    #: `chats.allowlist_for`. Keys are conversation IDs, never names: a channel rename
    #: must not silently un-monitor a conversation.
    allowlist: Allowlist = field(default_factory=lambda: Allowlist(()))
    #: The raw `SLACK_CHANNELS` entries. Kept as a floor on top of the allowlist so an
    #: explicit instruction in `.env` keeps working even if a discovery call fails.
    channel_ids: tuple[str, ...] = ()
    #: Threads with no activity in this many days leave the cursor's thread map.
    thread_window_days: int = 30
    #: Seconds this run may spend asleep obeying `Retry-After` before it gives up and
    #: stops cleanly. Bounded, because a scheduled sync that sleeps for an hour is a
    #: sync that never ran.
    rate_limit_budget_seconds: int = 120
    timeout_seconds: int = 30
    #: Injected in tests. Production leaves it None and gets `_http`.
    transport: Transport | None = None

    cursor: Cursor = None
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)
    rate_limited: bool = False
    #: Threads the re-poll budget did not reach this run. Reported, never silent.
    threads_deferred: int = 0

    #: What this run saw, allowed or not. `sync` writes these to `monitored_chat`.
    seen_chats: dict[str, Sighting] = field(default_factory=dict)
    #: Each run counts only the messages it read, so the tally accumulates.
    sightings_are_cumulative: bool = True

    #: Labelled like gmail/calendar/drive so a second workspace never needs a
    #: credential-row migration.
    label: str = "personal"

    _channel_names: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _user_names: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _users_loaded: bool = field(default=False, init=False, repr=False)
    _wait_spent: float = field(default=0.0, init=False, repr=False)

    @property
    def name(self) -> str:
        return f"slack:{self.label}"

    @property
    def chats_source(self) -> str:
        """Decisions are per workspace-conversation, not per label — `sync` reads this."""
        return "slack"

    def health(self) -> Health:
        if not self.token:
            return Health(name=self.name, ok=False, detail="SLACK_TOKEN is not set")
        try:
            self._call("auth.test", {})
        except SlackError as exc:
            # `invalid_auth`, `token_revoked`, `account_inactive` — Slack's own string, so
            # the Sources panel says the thing the owner has to go fix.
            return Health(name=self.name, ok=False, detail=f"slack: {exc.error}")
        except Exception as exc:  # noqa: BLE001 - rule 5: a health check never raises
            return Health(name=self.name, ok=False, detail=_safe(exc))
        return Health(name=self.name, ok=True)

    # ── fetching ──────────────────────────────────────────────────────────

    def fetch(self, since: Cursor) -> Iterator[SourceItem]:
        self.excluded = 0
        self.excluded_by_rule = {}
        self.rate_limited = False
        self.threads_deferred = 0
        self._wait_spent = 0.0
        self._channel_names = {}
        self._user_names = {}
        self._users_loaded = False

        marks = Watermarks.parse(since)
        # The cursor is published before the first message is yielded and re-published
        # after every channel, so a consumer that stops iterating early — or a rate limit
        # three channels in — still keeps the progress already made.
        self.cursor = marks.dumps()

        try:
            self.seen_chats = self.discover()
        except SlackRateLimited:
            self.rate_limited = True
            return
        except SlackError:
            # Discovery failing is not a reason to read nothing: `channel_ids` from `.env`
            # is an explicit instruction and does not depend on enumeration. Rule 5.
            self.seen_chats = {}

        visited: set[str] = set()
        try:
            for channel_id in self._channels_to_read():
                highest = marks.for_channel(channel_id)
                floor = highest
                for parent in self._history(channel_id, floor):
                    parent_ts = str(parent.get("ts") or "")
                    for message, thread_ts in self._thread(channel_id, parent, marks):
                        ts = str(message.get("ts") or "")
                        if not ts:
                            continue
                        if thread_ts:
                            # A reply is bounded by its own watermark inside `_replies`.
                            # The channel floor must NOT also apply: a late reply on an
                            # old parent legitimately sits below the channel's position,
                            # and that is the whole case pass two exists for.
                            marks.advance_thread(channel_id, thread_ts, ts)
                        else:
                            if not _newer(ts, floor):
                                continue
                            if _newer(ts, highest):
                                highest = ts
                        item = self._to_item(channel_id, message)
                        if item is not None:
                            self._count(channel_id)
                            yield item
                    if parent_ts:
                        visited.add(_thread_key(channel_id, parent_ts))
                # The channel finished cleanly. Its progress is durable from here, whatever
                # happens to the channels after it.
                marks.advance_channel(channel_id, highest)
                self.cursor = marks.dumps()

            yield from self._repoll_threads(marks, visited)
        except SlackRateLimited:
            # Stop cleanly, not loudly. Everything a completed channel read stays read;
            # the interrupted one is re-served next run, which content_hash makes free.
            self.rate_limited = True
        finally:
            cutoff = time.time() - self.thread_window_days * 86400
            marks.prune_threads(before=cutoff, keep=sorted(visited))
            self.cursor = marks.dumps()

    def _channels_to_read(self) -> list[str]:
        """Discovered conversations the owner said yes to, plus anything named in `.env`.

        The env entries are a floor rather than the source of truth: `chats.seed_from_env`
        has already turned them into `monitor` rows, so they are in the allowlist too —
        including them here is what keeps an explicit instruction working on a run where
        `users.conversations` failed.
        """
        out: list[str] = []
        for candidate in self.channel_ids:
            channel_id = candidate.strip()
            if channel_id and channel_id not in out:
                out.append(channel_id)
        for channel_id in self._channel_names:
            if channel_id not in out and self.allowlist.allows(
                title=channel_id, participants=[]
            ):
                out.append(channel_id)
        return out

    def _count(self, channel_id: str) -> None:
        sighting = self.seen_chats.get(channel_id)
        if sighting is not None:
            self.seen_chats[channel_id] = replace(sighting, messages=sighting.messages + 1)

    # ── discovery ─────────────────────────────────────────────────────────

    def discover(self) -> dict[str, Sighting]:
        """Every conversation the owner is a member of, as undecided sightings.

        Metadata only — an id, a name, a kind, a member count. No message is read here,
        and none is read anywhere for a conversation the allowlist has not admitted.
        """
        seen: dict[str, Sighting] = {}
        needs_user_names = False
        conversations: list[dict[str, Any]] = []

        for conversation in self._paged(
            "users.conversations",
            {"types": CONVERSATION_TYPES, "limit": PAGE_LIMIT, "exclude_archived": "true"},
            key="channels",
        ):
            channel_id = str(conversation.get("id") or "")
            if not channel_id:
                continue
            conversations.append(conversation)
            if conversation.get("is_im"):
                needs_user_names = True

        if needs_user_names:
            # One `users.list` for the whole run, and only when a DM actually needs a name
            # — the ids are useless to a reader and there is no cheaper way to a name.
            self._load_users()

        for conversation in conversations:
            channel_id = str(conversation["id"])
            display, kind = self._describe(conversation)
            self._channel_names[channel_id] = display
            seen[channel_id] = Sighting(
                key=channel_id,
                display_name=display,
                kind=kind,
                participants=_int_or_none(conversation.get("num_members")),
                # Counted as messages are read. A conversation nobody has chosen is
                # honestly reported as zero rather than guessed at, because counting it
                # would mean reading it.
                messages=0,
            )
        return seen

    def _describe(self, conversation: dict[str, Any]) -> tuple[str, str]:
        channel_id = str(conversation["id"])
        if conversation.get("is_im"):
            user_id = str(conversation.get("user") or "")
            return f"@{self._user_names.get(user_id, user_id or channel_id)}", "direct"
        name = str(conversation.get("name") or "").strip()
        return (f"#{name}" if name else f"#{channel_id}"), "group"

    def _load_users(self) -> None:
        if self._users_loaded:
            return
        self._users_loaded = True
        try:
            for member in self._paged("users.list", {"limit": PAGE_LIMIT}, key="members"):
                user_id = str(member.get("id") or "")
                if user_id:
                    self._user_names[user_id] = _display_name(member) or user_id
        except SlackRateLimited:
            raise
        except SlackError:
            # A name is a courtesy. Rule 5: the run continues on raw ids, and because
            # names are never hashed, the fallback cannot change a content_hash.
            pass

    # ── history and threads ───────────────────────────────────────────────

    def _history(self, channel_id: str, oldest: str) -> Iterator[dict[str, Any]]:
        """`conversations.history` for one channel, paginated to completion.

        Thread parents only — see the module docstring and docs/12 §4.
        """
        params = {"channel": channel_id, "limit": PAGE_LIMIT}
        if oldest:
            params["oldest"] = oldest
        yield from self._paged("conversations.history", params)

    def _thread(
        self, channel_id: str, parent: dict[str, Any], marks: Watermarks
    ) -> Iterator[tuple[dict[str, Any], str]]:
        """A history message, then its thread replies if it has any.

        Yields `(message, thread_ts)` — `thread_ts` is empty for the parent itself and the
        parent's ts for every reply, so the caller knows which watermark to move.

        The fan-out is keyed on `reply_count` rather than `thread_ts == ts`, because a
        parent carries `thread_ts` only once it has been replied to anyway, and a zero
        count must cost zero requests.
        """
        yield parent, ""

        parent_ts = str(parent.get("ts") or "")
        if not parent_ts or not parent.get("reply_count"):
            return

        oldest = marks.for_thread(channel_id, parent_ts)
        marks.register_thread(channel_id, parent_ts, oldest)
        for reply in self._replies(channel_id, parent_ts, oldest):
            yield reply, parent_ts

    def _replies(
        self, channel_id: str, parent_ts: str, oldest: str
    ) -> Iterator[dict[str, Any]]:
        params = {"channel": channel_id, "ts": parent_ts, "limit": PAGE_LIMIT}
        if oldest:
            params["oldest"] = oldest
        for reply in self._paged("conversations.replies", params):
            reply_ts = str(reply.get("ts") or "")
            if reply_ts == parent_ts:
                # docs/12 §4: `replies` re-serves the parent as its first item. History
                # already yielded it; a second copy would be a duplicate external_id.
                continue
            if not reply_ts or not _newer(reply_ts, oldest):
                continue
            yield reply

    def _repoll_threads(self, marks: Watermarks, visited: set[str]) -> Iterator[SourceItem]:
        """The second thread pass: threads whose parent did not surface this run.

        This is what closes the gap the module used to document as accepted. A reply
        landing today on a thread started before the cursor does not move its parent's
        `ts`, so `conversations.history` never re-serves that parent and the reply is
        invisible to pass one.
        """
        pending: list[tuple[str, str, str]] = []
        readable = set(self._channels_to_read())
        for channel_id, threads in marks.channels_of_threads().items():
            if channel_id not in readable:
                continue
            for parent_ts, watermark in threads:
                if _thread_key(channel_id, parent_ts) in visited:
                    continue
                pending.append((watermark, channel_id, parent_ts))

        # Oldest watermark first, so a map larger than the budget drains fairly instead of
        # starving the same tail every run.
        pending.sort(key=lambda row: _seconds(row[0]) or 0.0)
        self.threads_deferred = max(0, len(pending) - _THREAD_REPOLL_LIMIT)

        for watermark, channel_id, parent_ts in pending[:_THREAD_REPOLL_LIMIT]:
            for reply in self._replies(channel_id, parent_ts, watermark):
                ts = str(reply.get("ts") or "")
                marks.advance_thread(channel_id, parent_ts, ts)
                item = self._to_item(channel_id, reply)
                if item is not None:
                    self._count(channel_id)
                    yield item
            self.cursor = marks.dumps()

    # ── mapping ───────────────────────────────────────────────────────────

    def _to_item(self, channel_id: str, message: dict[str, Any]) -> SourceItem | None:
        if message.get("bot_id"):
            # Bots are Slack's no-reply@ layer: deploy notifications, standup prompts,
            # calendar reminders. Never a commitment, always volume.
            return None
        if str(message.get("subtype") or "") in NOISE_SUBTYPES:
            return None

        text = str(message.get("text") or "").strip()
        if not text:
            return None

        # D1. Before persistence — a pasted client thread in Slack is client
        # correspondence exactly as it is in a note.
        verdict = self.boundary.check(_EMAIL.findall(text))
        if not verdict.allowed:
            self.excluded += 1
            rule = verdict.matched_rule or "?"
            self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + 1
            return None

        ts = str(message["ts"])
        occurred_at = _iso_from_ts(ts)
        if occurred_at is None:
            return None

        user_id = str(message.get("user") or "")
        author = self._author_of(user_id)
        title = self._channel_names.get(channel_id) or f"#{channel_id}"

        return SourceItem(
            source=self.name,
            external_id=f"{channel_id}:{ts}",
            occurred_at=occurred_at,
            author=author,
            title=title,
            body_text=text,
            raw_json=json.dumps(
                {**message, "channel": channel_id}, sort_keys=True, default=str
            ),
            # Over the STABLE identity, not the displayed one. See the module docstring:
            # a resolved name can drift, and a hash that drifts rewrites an immutable row.
            content_hash=content_hash(
                author=user_id or None,
                title=channel_id,
                body_text=text,
                occurred_at=occurred_at,
            ),
        )

    def _author_of(self, user_id: str) -> str | None:
        if not user_id:
            return None
        if not self._user_names and not self._users_loaded:
            self._load_users()
        return self._user_names.get(user_id) or user_id

    # ── HTTP ──────────────────────────────────────────────────────────────

    def _paged(
        self, method: str, params: dict[str, str], *, key: str = "messages"
    ) -> Iterator[dict[str, Any]]:
        """Rows from a cursor-paginated Slack method, to completion."""
        next_cursor = ""
        seen_cursors: set[str] = set()
        while True:
            page = dict(params)
            if next_cursor:
                page["cursor"] = next_cursor
            payload = self._call(method, page)
            for row in payload.get(key) or []:
                if isinstance(row, dict):
                    yield row
            metadata = payload.get("response_metadata")
            next_cursor = str((metadata or {}).get("next_cursor") or "")
            if not next_cursor or next_cursor in seen_cursors:
                return
            seen_cursors.add(next_cursor)

    def _call(self, method: str, params: dict[str, str]) -> dict[str, Any]:
        """One Slack API call, with the `{"ok": false}` envelope turned into an exception."""
        transport = self.transport or self._http
        payload = transport(method, params)
        if not isinstance(payload, dict):
            raise SlackError("malformed response")
        if payload.get("ok"):
            return payload
        error = str(payload.get("error") or "unknown_error")
        if error == "ratelimited":
            raise SlackRateLimited(error)
        raise SlackError(error)

    def _http(self, method: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{API}/{method}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
        )
        attempt = 0
        while True:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    body = response.read() or b"{}"
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    # Slack says how long to wait and the old code threw that away, so a
                    # throttled run burned its retries against a wall it had been told
                    # the height of. Obey it, inside a budget the whole run shares.
                    delay = _retry_after(exc)
                    if delay is not None and self._wait_spent + delay <= (
                        self.rate_limit_budget_seconds
                    ):
                        self._wait_spent += delay
                        time.sleep(delay)
                        continue
                    return {"ok": False, "error": "ratelimited"}
                if exc.code >= 500 and attempt < len(BACKOFF_SECONDS):
                    time.sleep(BACKOFF_SECONDS[attempt])
                    attempt += 1
                    continue
                raise
            try:
                decoded = json.loads(body)
            except ValueError as exc:
                raise SlackError("malformed response") from exc
            if not isinstance(decoded, dict):
                raise SlackError("malformed response")
            return decoded


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    """Slack's `Retry-After`, in seconds, or None when it did not say."""
    headers = getattr(exc, "headers", None)
    raw = ""
    if headers is not None:
        try:
            raw = str(headers.get("Retry-After") or "")
        except Exception:  # noqa: BLE001 — a header bag that raises is a header bag with no answer
            raw = ""
    try:
        delay = float(raw)
    except ValueError:
        return None
    # A negative or absurd delay is Slack malfunctioning, not an instruction.
    return delay if 0 <= delay <= 300 else None


def _display_name(member: dict[str, Any]) -> str:
    profile = member.get("profile")
    if isinstance(profile, dict):
        for field_name in ("display_name", "real_name"):
            value = str(profile.get(field_name) or "").strip()
            if value:
                return value
    return str(member.get("real_name") or member.get("name") or "").strip()


def _ts_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if value}


def _thread_key(channel_id: str, parent_ts: str) -> str:
    return f"{channel_id}:{parent_ts}"


def _seconds(ts: str) -> float | None:
    try:
        return float(ts)
    except ValueError:
        return None


def _iso_from_ts(ts: str) -> str | None:
    """`"1753800000.000200"` → `"2025-07-29T14:40:00.000200+00:00"`.

    Only for `occurred_at`. The cursor keeps the raw string.
    """
    seconds = _seconds(ts)
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def _later(ts: str, other: str) -> str:
    return ts if _newer(ts, other) else other


def _newer(ts: str, than: str) -> bool:
    if not than:
        return True
    if not ts:
        return False
    left, right = _seconds(ts), _seconds(than)
    if left is None or right is None:
        return True
    return left > right


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return re.sub(r"(Bearer\s+)\S+", r"\1[redacted]", text)[:500]
