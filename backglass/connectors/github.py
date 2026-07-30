"""GitHub. Assigned issues and open pull requests are commitments with a URL attached.

An issue assigned to the owner, a PR they authored, or a review someone requested from
them are all obligations that already carry the two things extraction usually has to
guess at: who owes it and what it is attached to. So this connector is deliberately thin
— it does not try to read the discussion, it hands the extractor a structured statement
of the obligation and lets the ledger do the rest.

Shape of the integration, mirroring the Canvas connector:

  - REST v3 over stdlib urllib, `Authorization: Bearer <token>`,
    `Accept: application/vnd.github+json`.
  - One `GET /search/issues?q=involves:@me` query rather than three separate
    `assignee:`/`author:`/`review-requested:` queries. `involves:@me` is the union of
    exactly those three plus mentions, so one paginated query replaces three and cannot
    return the same issue twice — deduplicating across three cursors is the kind of
    bookkeeping that goes wrong quietly.
  - RFC 5988 `Link` pagination to completion, same as Canvas.
  - Serialized requests. GitHub's search endpoint has its own small per-minute quota;
    a one-at-a-time client stays under it.

Two decisions worth naming:

**Results are sorted `updated` ascending.** The default is descending by relevance, which
would make an early stop unsafe: if the quota runs out halfway through, the newest item
seen is *not* a watermark you can resume from, because older unfetched items would be
skipped forever. Ascending order makes "the newest thing I actually saw" a correct resume
point under any early exit, which is what the rate-limit path below relies on.

**The cursor keeps full timestamp precision.** tasks/lessons.md 2026-07-30: the Obsidian
connector truncated its watermark to whole seconds and re-read the entire vault every run,
which idempotency hid because nothing was written. The watermark stored here is the exact
`updated_at` string of the newest item yielded, and it is also what goes into the next
query's `updated:>` qualifier.
"""

from __future__ import annotations

import contextlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from backglass.connectors.base import Cursor, Health, SourceItem, content_hash
from backglass.connectors.boundary import Boundary

#: RFC 5988, same as Canvas. GitHub returns the next page only in this header.
_LINK = re.compile(r'<(?P<url>[^>]+)>;\s*rel="(?P<rel>[^"]+)"')

#: Bodies are prose written by other people and the extractor is charged by the token.
#: docs/02's cost model: an issue thread's first four thousand characters carry the
#: obligation; the rest is discussion.
BODY_CEILING = 4000

#: The boundary needs addresses and GitHub gives us logins, so the only place a client
#: address can appear is prose someone pasted in. Deliberately conservative — this exists
#: to catch a forwarded email quoted into an issue, not to parse RFC 5322.
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


@dataclass(kw_only=True)
class GithubConnector:
    token: str
    boundary: Boundary
    api_root: str = "https://api.github.com"
    timeout_seconds: int = 30

    cursor: Cursor = None
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)
    rate_limit_remaining: float | None = None
    #: True when a run stopped early because the quota was spent. Not an error: the
    #: cursor is still correct, the next run resumes from it.
    rate_limited: bool = False

    @property
    def name(self) -> str:
        return "github"

    def health(self) -> Health:
        if not self.token:
            return Health(name=self.name, ok=False, detail="GITHUB_TOKEN is not set")
        try:
            self._get_url(f"{self.api_root.rstrip('/')}/user")
        except PermissionError as exc:
            return Health(
                name=self.name,
                ok=False,
                # docs/07: auth expiry is a product state, so say what to do about it.
                # GitHub answers 401 for a revoked token, an expired token, and a token
                # whose fine-grained permissions were narrowed — all fixed the same way.
                detail=(
                    f"token revoked or expired — GitHub rejected it ({exc}). "
                    "Regenerate the personal access token and store it in `credential`."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return Health(name=self.name, ok=False, detail=_safe(exc))
        return Health(name=self.name, ok=True)

    # ── fetching ──────────────────────────────────────────────────────────

    def fetch(self, since: Cursor) -> Iterator[SourceItem]:
        """Issues and pull requests the owner is on the hook for, oldest change first."""
        self.excluded = 0
        self.excluded_by_rule = {}
        self.rate_limited = False
        latest = str(since or "")

        url: str | None = self._search_url(since)
        seen: set[str] = set()
        while url and url not in seen:
            seen.add(url)
            payload, headers = self._get_url(url)
            for raw in _results(payload):
                item = self._to_item(raw)
                if item is None:
                    continue
                if since and item.occurred_at <= str(since):
                    continue
                latest = max(latest, item.occurred_at)
                yield item

            if self._quota_spent(headers):
                # Stop cleanly rather than raising. Ascending order means `latest` is a
                # safe resume point: everything not yet seen is newer than it.
                self.rate_limited = True
                break
            url = _next_link(headers.get("Link", ""))

        if latest:
            self.cursor = latest

    def _search_url(self, since: Cursor) -> str:
        query = "involves:@me"
        if since:
            # Strictly greater-than, at full precision, so an unchanged issue is never
            # fetched a second time.
            query = f"{query} updated:>{since}"
        params = {
            "q": query,
            "sort": "updated",
            "order": "asc",
            "per_page": "100",
        }
        return f"{self.api_root.rstrip('/')}/search/issues?{urllib.parse.urlencode(params)}"

    def _to_item(self, raw: dict[str, Any]) -> SourceItem | None:
        number = raw.get("number")
        updated = raw.get("updated_at")
        if number is None or not updated:
            return None

        repo = _repo_full_name(raw)
        occurred_at = str(updated).replace("Z", "+00:00")
        subject = str(raw.get("title") or "")
        author = str((raw.get("user") or {}).get("login") or "") or None
        kind = "pull request" if raw.get("pull_request") else "issue"
        labels = [str(label.get("name")) for label in raw.get("labels") or [] if _named(label)]
        assignees = [
            str(person.get("login")) for person in raw.get("assignees") or [] if _login(person)
        ]

        body = str(raw.get("body") or "").strip()[:BODY_CEILING]
        lines = [
            f"{repo}#{number} ({kind}, {raw.get('state') or 'open'}): {subject}",
            f"Assigned to: {', '.join(assignees) if assignees else 'nobody'}",
        ]
        if labels:
            lines.append(f"Labels: {', '.join(labels)}")
        if body:
            lines.append("")
            lines.append(body)
        body_text = "\n".join(lines)

        # docs/08 D1, before persistence. GitHub identities are logins, not addresses, so
        # the denylist has nothing to match on in the metadata; the one place a client
        # address can reach the ledger through this source is prose pasted into a title or
        # body, and that is what gets checked.
        verdict = self.boundary.check(_EMAIL.findall(f"{subject}\n{body}"))
        if not verdict.allowed:
            self.excluded += 1
            rule = verdict.matched_rule or "?"
            self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + 1
            return None

        title = f"[{repo}] {subject}"
        return SourceItem(
            source=self.name,
            external_id=f"{repo}#{number}",
            occurred_at=occurred_at,
            author=author,
            title=title,
            body_text=body_text,
            raw_json=json.dumps(raw, sort_keys=True),
            content_hash=content_hash(
                author=author, title=title, body_text=body_text, occurred_at=occurred_at
            ),
        )

    # ── HTTP ──────────────────────────────────────────────────────────────

    def _get_url(self, url: str) -> tuple[Any, dict[str, str]]:
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                headers = {k: v for k, v in response.headers.items()}
                return json.loads(response.read() or b"{}"), headers
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise PermissionError(f"HTTP {exc.code} {exc.reason}") from exc
            raise

    def _quota_spent(self, headers: dict[str, str]) -> bool:
        remaining = headers.get("X-RateLimit-Remaining")
        if remaining is None:
            return False
        with contextlib.suppress(ValueError):
            self.rate_limit_remaining = float(remaining)
        return self.rate_limit_remaining == 0


def _results(payload: Any) -> list[dict[str, Any]]:
    """The search endpoint wraps its page in `items`; the plain list endpoints do not."""
    rows = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _repo_full_name(raw: dict[str, Any]) -> str:
    repository = raw.get("repository")
    if isinstance(repository, dict) and repository.get("full_name"):
        return str(repository["full_name"])
    # Search results carry only the API URL: https://api.github.com/repos/owner/name
    url = str(raw.get("repository_url") or "")
    _, _, tail = url.partition("/repos/")
    return tail or "unknown/unknown"


def _named(label: Any) -> bool:
    return isinstance(label, dict) and bool(label.get("name"))


def _login(person: Any) -> bool:
    return isinstance(person, dict) and bool(person.get("login"))


def _next_link(header: str) -> str | None:
    for match in _LINK.finditer(header or ""):
        if match.group("rel") == "next":
            return match.group("url")
    return None


def _safe(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return re.sub(r"(Bearer\s+)\S+", r"\1[redacted]", text)[:500]
