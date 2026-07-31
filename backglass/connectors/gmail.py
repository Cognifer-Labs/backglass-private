"""The Gmail connector. docs/07 §Gmail.

  - Incremental via `historyId`. Full scan only on first run or cursor loss.
  - Scope `gmail.readonly` and nothing else. The system never sends or modifies, and
    docs/10 §Email delivery makes that true by construction: the brief goes out through a
    transactional provider, so send capability is never in this credential's reach.
  - Quoted history is stripped before hashing, so a forty-message thread does not produce
    forty near-identical items.
  - The boundary check runs here, before the item is yielded — never downstream.

The Gmail API client is injected rather than constructed, so tests drive the connector
against recorded fixtures and no test touches the network.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from backglass.connectors.base import Cursor, Health, SourceItem, content_hash
from backglass.connectors.boundary import Boundary, addresses_in
from backglass.extract import quoting

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

#: Gmail's own categories for bulk mail. Excluded from the full scan so a first run does
#: not pull ten thousand newsletters just to have the rule layer drop them one at a time.
#: This is a fetch filter, not a triage rule — it saves API calls, not model calls.
DEFAULT_QUERY = "-in:chats -category:promotions -category:social -category:forums"

_TAG = re.compile(r"<[^>]+>")


def strip_quoted(body: str) -> str:
    """Remove quoted history and the signature block.

    docs/07 requires this to happen before hashing. The reason is the cost model: without
    it, every reply in a thread is a new content_hash, so every reply is a new
    source_item, and every one of them gets a model call for text that was already read.

    The patterns live in `backglass.extract.quoting` — vendored from talon and
    email-reply-parser per docs/12 §3 — because every text source has quoted history in
    it, not just Gmail. This stays as the connector's seam; only the battery moved.
    """
    return quoting.clean(body)


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {h["name"]: h["value"] for h in payload.get("headers", []) if "name" in h}


def _decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _body_text(payload: dict[str, Any]) -> str:
    """Prefer text/plain. Fall back to text/html with tags stripped."""
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data:
            if mime == "text/plain":
                plain.append(_decode(data))
            elif mime == "text/html":
                html.append(_decode(data))
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    if plain:
        return "\n".join(plain)
    if html:
        text = _TAG.sub(" ", "\n".join(html))
        return re.sub(r"[ \t]{2,}", " ", text)
    return ""


def occurred_at_of(headers: dict[str, str], internal_date_ms: str | int | None) -> str:
    """When the message was sent, with the sender's UTC offset preserved.

    The offset is load-bearing, not decoration. CLAUDE.md rule 4 resolves relative dates
    against this timestamp, and "by Friday" written at 19:00 in Phoenix is a different
    Friday from "by Friday" written at 07:00 the next morning in Coimbatore. The `Date`
    header carries the sender's offset; `internalDate` is UTC epoch milliseconds and has
    thrown that information away, so it is only a fallback.
    """
    raw = headers.get("Date")
    if raw:
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.replace(microsecond=0).isoformat()
        except (TypeError, ValueError):
            pass
    if internal_date_ms is not None:
        seconds = int(internal_date_ms) / 1000
        return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=0).isoformat()
    raise ValueError("message has neither a parseable Date header nor an internalDate")


@dataclass
class GmailConnector:
    """One mailbox.

    `label` distinguishes accounts: `credential` is UNIQUE(user_id, source), so two
    mailboxes cannot both be `source='gmail'`. They become `gmail:personal` and
    `gmail:asu`. See tasks/todo.md §Deviations #2.
    """

    label: str
    service: Any
    boundary: Boundary
    query: str = DEFAULT_QUERY
    max_results: int = 500

    #: Only final once `fetch()` has been exhausted.
    cursor: Cursor = None
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)
    _error: str | None = None

    @property
    def name(self) -> str:
        return f"gmail:{self.label}"

    def health(self) -> Health:
        if self._error:
            return Health(name=self.name, ok=False, detail=self._error)
        try:
            self.service.users().getProfile(userId="me").execute()
        except Exception as exc:  # noqa: BLE001 - any failure is a health failure
            return Health(name=self.name, ok=False, detail=_safe_error(exc))
        return Health(name=self.name, ok=True)

    # ────────────────────────────────────────────────────────────── fetching

    def fetch(self, since: Cursor) -> Iterator[SourceItem]:
        """Yield new items. Signature per the Protocol in docs/07.

        `self.cursor`, `self.excluded` and `self.excluded_by_rule` are only meaningful
        after the iterator is exhausted — the connector streams so a first-run backfill
        does not have to fit in memory.
        """
        self.excluded = 0
        self.excluded_by_rule = {}
        message_ids, new_cursor = self._message_ids(since)
        for message_id in message_ids:
            item = self._fetch_one(message_id)
            if item is not None:
                yield item
        self.cursor = new_cursor or since

    def _message_ids(self, since: Cursor) -> tuple[list[str], Cursor]:
        if since:
            try:
                return self._incremental(since)
            except Exception as exc:  # noqa: BLE001
                # docs/07: full scan only on first run *or cursor loss*. Gmail expires
                # historyIds after about a week, and a laptop that slept through a
                # vacation is the normal way to hit this, not an error.
                self._error = None
                _ = exc
        return self._full_scan()

    def _incremental(self, since: str) -> tuple[list[str], Cursor]:
        ids: list[str] = []
        cursor: Cursor = since
        request: Any = (
            self.service.users()
            .history()
            .list(userId="me", startHistoryId=since, historyTypes=["messageAdded"])
        )
        while request is not None:
            response = request.execute()
            cursor = str(response.get("historyId") or cursor)
            for record in response.get("history", []) or []:
                for added in record.get("messagesAdded", []) or []:
                    message = added.get("message", {})
                    if "id" in message:
                        ids.append(message["id"])
            request = self.service.users().history().list_next(request, response)
        return list(dict.fromkeys(ids)), cursor

    def _full_scan(self) -> tuple[list[str], Cursor]:
        ids: list[str] = []
        cursor: Cursor = None
        request: Any = (
            self.service.users()
            .messages()
            .list(userId="me", q=self.query, maxResults=self.max_results)
        )
        while request is not None:
            response = request.execute()
            for message in response.get("messages", []) or []:
                ids.append(message["id"])
            request = self.service.users().messages().list_next(request, response)
        profile = self.service.users().getProfile(userId="me").execute()
        if profile.get("historyId"):
            cursor = str(profile["historyId"])
        return list(dict.fromkeys(ids)), cursor

    def _fetch_one(self, message_id: str) -> SourceItem | None:
        raw = (
            self.service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        payload = raw.get("payload", {}) or {}
        headers = _headers(payload)

        # D1/D4. Before persistence, before hashing, before any model call. Everything
        # below this line has already been cleared by the boundary.
        verdict = self.boundary.check(addresses_in(headers))
        if not verdict.allowed:
            self.excluded += 1
            rule = verdict.matched_rule or "?"
            self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + 1
            return None

        body = strip_quoted(_body_text(payload))
        occurred_at = occurred_at_of(headers, raw.get("internalDate"))
        author = headers.get("From")
        title = headers.get("Subject")

        return SourceItem(
            source=self.name,
            external_id=message_id,
            occurred_at=occurred_at,
            author=author,
            title=title,
            body_text=body,
            raw_json=json.dumps(
                {
                    "thread_id": raw.get("threadId"),
                    "label_ids": raw.get("labelIds", []),
                    # Kept so the D6 purge can re-evaluate the boundary over stored items
                    # after the denylist is corrected. Bodies are not duplicated here.
                    "headers": headers,
                },
                sort_keys=True,
            ),
            content_hash=content_hash(
                author=author, title=title, body_text=body, occurred_at=occurred_at
            ),
        )


def _safe_error(exc: Exception) -> str:
    """A message safe to store and display.

    docs/08: body_text is never included in error reports or exception traces, and tokens
    never appear in logs. Google's client raises exceptions whose str() can contain the
    request URL, which carries the access token as a query parameter on some transports.
    """
    text = f"{type(exc).__name__}: {exc}"
    text = re.sub(r"(access_token|key|token)=[^&\s]+", r"\1=[redacted]", text)
    return text[:500]
