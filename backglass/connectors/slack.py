"""Slack. A deliberately narrow connector: the handful of conversations the owner names.

**Scope is bounded on purpose.** This connector never enumerates conversations. It reads
only the channel IDs it was constructed with — a DM, a founders channel, whatever the
owner explicitly listed — via `conversations.history`. A personal second brain does not
need to crawl a workspace; the commitments live in three or four threads, and crawling the
rest would spend the docs/02 cost model on standups and emoji. If the owner wants a new
channel read, that is a config edit, not a discovery pass.

Wire details:

  - `POST-less` GET to `https://slack.com/api/conversations.history`, `Authorization: Bearer`.
  - `oldest=<cursor>`, `limit=200`, paginated via `response_metadata.next_cursor` to
    completion.
  - Slack answers 200 with `{"ok": false, "error": "..."}` for auth and quota failures, so
    the envelope is checked on every call rather than trusting the HTTP status.

**Cursor.** The stored cursor is a single Slack `ts` string — the highest one seen across
every configured channel — kept at full precision. `ts` is a fractional-seconds string
(`"1753800000.000200"`); truncating it is the Obsidian watermark bug in tasks/lessons.md
wearing a different hat, so it is never parsed-and-reformatted for storage, only for
comparison.

One watermark for N channels is a trade-off taken knowingly: the same `oldest` is passed to
every channel, so a busy channel can re-serve messages a quiet one has already been read
past. That costs one HTTP round trip and zero model calls, because `content_hash` makes a
re-served message a no-op write (docs/03). The alternative — a per-channel cursor map
encoded into the opaque cursor string — buys nothing at this scale and adds a parser.

**Authors.** `author` is the raw Slack user ID (`U123ABC`). No `users.info` call is made to
resolve display names: it would be one extra request per distinct author per run purely for
cosmetics, and the entity resolution layer maps identifiers to people anyway.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from backglass.connectors.base import Cursor, Health, SourceItem, content_hash
from backglass.connectors.boundary import Boundary

API = "https://slack.com/api"

#: docs/07's serialize-and-back-off rule. Slack's own guidance is the same: one request at
#: a time, obey the retry delay.
BACKOFF_SECONDS = (2, 8, 30)

#: `conversations.history` caps at 1000; 200 is Slack's recommended page size.
PAGE_LIMIT = "200"

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
    """Quota exhausted. Not a failure — a reason to stop early and keep the cursor safe."""


@dataclass(kw_only=True)
class SlackConnector:
    token: str
    channel_ids: tuple[str, ...]
    boundary: Boundary
    timeout_seconds: int = 30
    #: Injected in tests. Production leaves it None and gets `_http`.
    transport: Transport | None = None

    cursor: Cursor = None
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)
    rate_limited: bool = False

    @property
    def name(self) -> str:
        return "slack"

    def health(self) -> Health:
        if not self.token:
            return Health(name=self.name, ok=False, detail="SLACK_TOKEN is not set")
        if not self.channel_ids:
            return Health(
                name=self.name,
                ok=False,
                detail="no channel IDs configured; this connector reads only named channels",
            )
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

        oldest = str(since or "")
        highest = oldest

        for channel_id in self.channel_ids:
            try:
                for message in self._history(channel_id, oldest):
                    ts = str(message.get("ts") or "")
                    if not ts or not _newer(ts, oldest):
                        continue
                    if _newer(ts, highest):
                        highest = ts
                    item = self._to_item(channel_id, message)
                    if item is not None:
                        yield item
            except SlackRateLimited:
                # Stop cleanly, not loudly. The cursor stays where the run started so the
                # unread tail of this channel is re-served next run; content_hash makes the
                # already-seen part free.
                self.rate_limited = True
                self.cursor = since
                return

        if highest:
            self.cursor = highest

    def _history(self, channel_id: str, oldest: str) -> Iterator[dict[str, Any]]:
        """`conversations.history` for one channel, paginated to completion."""
        params = {"channel": channel_id, "limit": PAGE_LIMIT}
        if oldest:
            params["oldest"] = oldest
        next_cursor = ""
        seen_cursors: set[str] = set()
        while True:
            page = dict(params)
            if next_cursor:
                page["cursor"] = next_cursor
            payload = self._call("conversations.history", page)
            for message in payload.get("messages") or []:
                if isinstance(message, dict):
                    yield message
            metadata = payload.get("response_metadata")
            next_cursor = str((metadata or {}).get("next_cursor") or "")
            if not next_cursor or next_cursor in seen_cursors:
                return
            seen_cursors.add(next_cursor)

    def _to_item(self, channel_id: str, message: dict[str, Any]) -> SourceItem | None:
        if message.get("bot_id"):
            # Bots are Slack's no-reply@ layer: deploy notifications, standup prompts,
            # calendar reminders. Never a commitment, always volume.
            return None
        if message.get("subtype"):
            # channel_join, channel_topic, thread_broadcast of a join, and friends.
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
        author = str(message.get("user") or "") or None
        title = f"#{channel_id}"

        return SourceItem(
            source=self.name,
            external_id=f"{channel_id}:{ts}",
            occurred_at=occurred_at,
            author=author,
            title=title,
            body_text=text,
            raw_json=json.dumps(message, sort_keys=True),
            content_hash=content_hash(
                author=author, title=title, body_text=text, occurred_at=occurred_at
            ),
        )

    # ── HTTP ──────────────────────────────────────────────────────────────

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
        for attempt, pause in enumerate((0, *BACKOFF_SECONDS)):
            if pause:
                time.sleep(pause)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    body = response.read() or b"{}"
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    # Slack's HTTP form of the same signal as the `ratelimited` envelope.
                    return {"ok": False, "error": "ratelimited"}
                if exc.code >= 500 and attempt < len(BACKOFF_SECONDS):
                    continue
                raise
            try:
                decoded = json.loads(body)
            except ValueError as exc:
                raise SlackError("malformed response") from exc
            if not isinstance(decoded, dict):
                raise SlackError("malformed response")
            return decoded
        raise TimeoutError("slack did not answer")


def _iso_from_ts(ts: str) -> str | None:
    """`"1753800000.000200"` → `"2026-07-29T14:40:00+00:00"`.

    Only for `occurred_at`. The cursor keeps the raw string.
    """
    try:
        seconds = float(ts)
    except ValueError:
        return None
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def _newer(ts: str, than: str) -> bool:
    if not than:
        return True
    try:
        return float(ts) > float(than)
    except ValueError:
        return True


def _safe(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return re.sub(r"(Bearer\s+)\S+", r"\1[redacted]", text)[:500]
