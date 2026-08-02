"""Canvas. docs/07 §Canvas — "Optional and last. The original request that seeded this
project."

  - Canvas REST API with a personal access token, `Authorization: Bearer`.
  - `GET /api/v1/courses?enrollment_state=active&per_page=100`
  - `GET /api/v1/courses/:id/assignments?include[]=submission&per_page=100`
  - Follow RFC 5988 `Link` header pagination to completion.
  - Leaky-bucket quota: read `X-Rate-Limit-Remaining`, back off on 403. **Serialize
    requests**; "a one-at-a-time client is unlikely to be throttled, which is a good
    reason not to parallelize."

An assignment with a due date and no submission is a commitment the owner has made to an
institution, so these are emitted with enough structure that extraction produces an
`i_owe` without having to infer one from prose. The body text is deliberately terse for
the same reason — docs/02's cost model, and a Canvas description is usually boilerplate.

docs/07 also warns: "Some institutions disable student-generated tokens. Check Account →
Settings → Approved Integrations before building. If absent, the fallback is the ICS feed,
which loses submission state." `health()` says which of those you are in.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from backglass.connectors.base import Cursor, Health, SourceItem, content_hash
from backglass.connectors.boundary import Boundary

#: RFC 5988. Canvas paginates everything and returns the next page only in this header.
_LINK = re.compile(r'<(?P<url>[^>]+)>;\s*rel="(?P<rel>[^"]+)"')

#: docs/07: back off on 403, which is how Canvas signals the leaky bucket is empty.
BACKOFF_SECONDS = (2, 8, 30)


@dataclass
class CanvasConnector:
    base_url: str
    token: str
    boundary: Boundary
    label: str = "canvas"
    timeout_seconds: int = 30

    cursor: Cursor = None
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)
    rate_limit_remaining: float | None = None

    @property
    def name(self) -> str:
        return f"canvas:{self.label}"

    def health(self) -> Health:
        if not self.base_url or not self.token:
            return Health(
                name=self.name, ok=False, detail="CANVAS_BASE_URL or CANVAS_TOKEN is not set"
            )
        try:
            self._get("/api/v1/users/self")
        except PermissionError:
            return Health(
                name=self.name,
                ok=False,
                # docs/07's documented failure mode, named so it is not mistaken for a
                # transient error.
                detail=(
                    "token rejected. Some institutions disable student-generated tokens — "
                    "check Account → Settings → Approved Integrations. The fallback is the "
                    "ICS feed, which loses submission state."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return Health(name=self.name, ok=False, detail=_safe(exc))
        return Health(name=self.name, ok=True)

    # ── fetching ──────────────────────────────────────────────────────────

    def fetch(self, since: Cursor) -> Iterator[SourceItem]:
        """Assignments across active courses.

        Serialized on purpose. docs/07: "a one-at-a-time client is unlikely to be
        throttled, which is a good reason not to parallelize." The rest of the sync is
        concurrent; this connector is the exception and it is a deliberate one.
        """
        self.excluded = 0
        self.excluded_by_rule = {}
        latest = str(since or "")

        courses = self._paginate(
            "/api/v1/courses", {"enrollment_state": "active", "per_page": "100"}
        )
        for course in courses:
            course_id = course.get("id")
            if course_id is None:
                continue
            assignments = self._paginate(
                f"/api/v1/courses/{course_id}/assignments",
                {"include[]": "submission", "per_page": "100"},
            )
            for assignment in assignments:
                item = self._to_item(course, assignment)
                if item is None:
                    continue
                # Boundary-equal items are re-read, not skipped. Unlike github, which
                # filters server-side with `updated:>` at full precision, this watermark
                # is Canvas's own mutable `updated_at` at whole-second resolution, so a
                # bulk administrative edit can stamp a dozen assignments with the exact
                # second already stored. Dropping those loses them permanently; re-reading
                # them costs zero writes, because content_hash short-circuits an unchanged
                # row (tasks/lessons.md 2026-07-30 makes the same trade for Obsidian).
                if since and item.occurred_at < str(since):
                    continue
                latest = max(latest, item.occurred_at)
                yield item

        if latest:
            self.cursor = latest

    def _to_item(self, course: dict[str, Any], assignment: dict[str, Any]) -> SourceItem | None:
        due = assignment.get("due_at")
        if not due:
            return None  # no date, no commitment
        submission = assignment.get("submission") or {}
        if submission.get("submitted_at") or submission.get("workflow_state") == "graded":
            return None  # already delivered; not an open obligation

        updated = str(assignment.get("updated_at") or due).replace("Z", "+00:00")
        course_name = str(course.get("name") or f"Course {course.get('id')}")
        title = str(assignment.get("name") or "Assignment")

        verdict = self.boundary.check([str(course.get("account_id") or "")])
        if not verdict.allowed:
            self.excluded += 1
            rule = verdict.matched_rule or "?"
            self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + 1
            return None

        # Terse on purpose. The due date is the fact that matters and it is structured;
        # a Canvas description is usually rubric boilerplate and paying to extract from it
        # is exactly the cost mistake docs/02 warns about.
        body = f"{course_name}: {title} is due {str(due).replace('Z', '+00:00')}."
        return SourceItem(
            source=self.name,
            external_id=f"{course.get('id')}:{assignment.get('id')}",
            occurred_at=updated,
            author=course_name,
            title=f"{course_name} — {title}",
            body_text=body,
            raw_json=json.dumps(
                {
                    "course_id": course.get("id"),
                    "assignment_id": assignment.get("id"),
                    "due_at": due,
                    "points_possible": assignment.get("points_possible"),
                    "html_url": assignment.get("html_url"),
                },
                sort_keys=True,
            ),
            content_hash=content_hash(
                author=course_name, title=title, body_text=body, occurred_at=updated
            ),
        )

    # ── HTTP, with the quota rules docs/07 specifies ──────────────────────

    def _paginate(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        """Follow RFC 5988 `Link` headers to completion."""
        url: str | None = f"{self.base_url.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        while url and url not in seen:
            seen.add(url)
            payload, headers = self._get_url(url)
            if isinstance(payload, list):
                out.extend(item for item in payload if isinstance(item, dict))
            url = _next_link(headers.get("Link", ""))
        return out

    def _get(self, path: str) -> Any:
        payload, _ = self._get_url(f"{self.base_url.rstrip('/')}{path}")
        return payload

    def _get_url(self, url: str) -> tuple[Any, dict[str, str]]:
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
                    headers = {k: v for k, v in response.headers.items()}
                    remaining = headers.get("X-Rate-Limit-Remaining")
                    if remaining:
                        with contextlib.suppress(ValueError):
                            self.rate_limit_remaining = float(remaining)
                    return json.loads(response.read() or b"[]"), headers
            except urllib.error.HTTPError as exc:
                if exc.code == 403 and attempt < len(BACKOFF_SECONDS):
                    continue  # leaky bucket empty; back off and retry
                if exc.code in (401, 403):
                    raise PermissionError("canvas rejected the token") from exc
                raise
        raise TimeoutError("canvas rate limit did not clear")


def _next_link(header: str) -> str | None:
    for match in _LINK.finditer(header or ""):
        if match.group("rel") == "next":
            return match.group("url")
    return None


def _safe(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return re.sub(r"(Bearer\s+)\S+", r"\1[redacted]", text)[:500]
