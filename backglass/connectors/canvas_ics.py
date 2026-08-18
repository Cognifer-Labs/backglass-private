"""Canvas over its published calendar feed, for institutions that disable student tokens.

docs/07 §Canvas warned about this and it is what ASU does: Account → Settings →
Approved Integrations renders "+ New Access Token" inert, with "Your Canvas administrators
have chosen to limit your ability to generate your own access token." The API connector's
`health()` names the fallback, and this is it.

**What this is not.** It is not a way around that decision. Canvas publishes a per-user
ICS feed from Calendar → Calendar Feed; this reads the document the institution already
hands out. There is no session cookie, no scraping and no borrowed token here, and if the
feed URL is revoked this connector stops working, which is the correct behaviour.

**Why not just subscribe in Calendar.app.** Because `apple_calendar` would collect it and
tier 0 would immediately drop it — docs/02 rules calendar invites out of extraction, and
the owner's 200 `calendar:asu` rows are the proof: every one kept, zero extracted. Read
that way an assignment feeds the capacity model and never becomes a commitment, so it
appears on the schedule and never in the brief, the due-today list, or the planner's work
selection. Emitting `canvas:ics` instead routes the same facts through extraction, which
is the entire reason this file exists rather than a line in `.env`.

**What it cannot recover, stated once so nobody mistakes this for the API path.** The feed
carries no submission state. `canvas.py` drops anything already submitted or graded, and
that filter is the most valuable thing the API gives; here it is absent, so work already
handed in keeps reading as an open obligation until the owner closes it. That is a real
downgrade and the reason `canvas.py` stays the preferred connector: if a token is ever
granted, set `CANVAS_TOKEN` and turn this one off.

**The feed's own shape.** Canvas emits one VEVENT per assignment *and* per calendar event,
distinguished only by the UID prefix — `event-assignment-<id>` against
`event-calendar-event-<id>`. Only assignments are obligations, so only they are emitted;
a class meeting is what `apple_calendar` is for. RFC 5545 line folding, TEXT escaping and
both DTSTART forms are handled here rather than by a dependency, because the subset in
play is small and pinning a calendar library for it is the larger cost.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from backglass.connectors.base import Cursor, Health, SourceItem, content_hash, safe_error
from backglass.connectors.boundary import Boundary

#: Canvas's own discriminator. Everything else in the feed is a calendar event, which is
#: `apple_calendar`'s job and not an obligation.
ASSIGNMENT_UID = re.compile(r"event-assignment-(\d+)")

#: A folded line continues when the next begins with a space or tab (RFC 5545 §3.1).
_FOLD = re.compile(r"\r?\n[ \t]")


def _unfold(text: str) -> list[str]:
    """RFC 5545 line unfolding, before anything is parsed.

    Canvas folds at 75 octets, which lands mid-word inside any assignment title longer
    than a few words — parsing without unfolding first silently truncates exactly the
    long titles a student most needs to recognise.
    """
    return _FOLD.sub("", text.replace("\r\n", "\n")).split("\n")


def _unescape(value: str) -> str:
    """TEXT escaping, RFC 5545 §3.3.11. Order matters: backslash last, or `\\;` becomes
    a literal semicolon and then loses its own escape."""
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _parse_dt(value: str) -> str | None:
    """A DTSTART in either form, as an ISO-8601 string.

    `DTSTART:20260828T065900Z` is an instant. `DTSTART;VALUE=DATE:20260828` is an all-day
    assignment — Canvas emits these for anything due "by end of day" — and it is rendered
    at local midnight rather than invented as a time, because docs/04's rule for a plan
    with a day and no hour is that it is not placed. Returning a fake hour here would
    launder a guess into the ledger as a fact.
    """
    raw = value.strip()
    try:
        if raw.endswith("Z"):
            stamp = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            return stamp.isoformat()
        if len(raw) == 8:
            return datetime.strptime(raw, "%Y%m%d").date().isoformat()
        return datetime.strptime(raw[:15], "%Y%m%dT%H%M%S").isoformat()
    except ValueError:
        return None


def _events(text: str) -> Iterator[dict[str, str]]:
    """Every VEVENT in the document, as flat property maps.

    Parameters are dropped from the key (`DTSTART;VALUE=DATE` becomes `DTSTART`) but kept
    in the value lookup for `_parse_dt`, which needs the length to tell the two forms
    apart. Unknown properties are carried through rather than filtered: a feed that grows
    a field should not need this parser changed to keep working.
    """
    current: dict[str, str] | None = None
    for line in _unfold(text):
        stripped = line.strip()
        if stripped == "BEGIN:VEVENT":
            current = {}
            continue
        if stripped == "END:VEVENT":
            if current:
                yield current
            current = None
            continue
        if current is None or ":" not in stripped:
            continue
        name, _, value = stripped.partition(":")
        current[name.split(";")[0].upper()] = value
    # A truncated download ends mid-event. Dropping the partial is correct: half an
    # assignment is not an assignment, and the next sync re-reads the whole document.


@dataclass
class CanvasIcsConnector:
    """The published Canvas calendar feed. No token, no cursor state on the server."""

    feed_url: str
    boundary: Boundary
    label: str = "ics"
    timeout_seconds: int = 30

    cursor: Cursor = None
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return f"canvas:{self.label}"

    def health(self) -> Health:
        if not self.feed_url:
            return Health(name=self.name, ok=False, detail="CANVAS_ICS_URL is not set")
        try:
            body = self._get()
        except Exception as exc:  # noqa: BLE001 — URLError, HTTPError, decode
            return Health(name=self.name, ok=False, detail=self._redact(safe_error(exc)))
        if "BEGIN:VCALENDAR" not in body:
            # A revoked or mistyped feed URL returns a Canvas HTML page with 200, which
            # would otherwise parse to zero events and read as "no assignments" forever.
            return Health(
                name=self.name,
                ok=False,
                detail=(
                    "the URL did not return a calendar. Canvas serves an HTML page rather "
                    "than a 404 for a reset feed — re-copy it from Calendar → Calendar Feed."
                ),
            )
        return Health(name=self.name, ok=True)

    def upstream_count(self) -> int | None:
        """Assignments the feed currently publishes — see `base.Countable`.

        Exact for the same reason `fetch` needs no server-side filter: the feed is one
        document and this reads all of it, applying the same `_to_item` rule so a class
        meeting is not counted as an obligation the ledger is missing. The watermark is
        not applied; the question is what the feed holds, not what is new.

        Worth having here specifically because this connector runs the *degraded* Canvas
        path — the ICS feed carries no submission state, so it is the source most likely
        to drift quietly, and the one whose silence is hardest to interpret by eye.
        """
        try:
            return sum(1 for event in _events(self._get()) if self._to_item(event))
        except Exception:  # noqa: BLE001 — a revoked feed is unknown, not zero
            return None

    def fetch(self, since: Cursor) -> Iterator[SourceItem]:
        """Assignments in the feed, newest-first watermark on the due date.

        The feed is a whole document with no server-side filter, so `since` bounds what is
        *emitted*, not what is fetched — there is nothing cheaper to ask for. Items at the
        watermark are re-read for `canvas.py`'s reason: an unchanged row costs zero writes
        because `content_hash` short-circuits it, and dropping it loses it permanently.
        """
        self.excluded = 0
        self.excluded_by_rule = {}
        latest = str(since or "")

        for event in _events(self._get()):
            item = self._to_item(event)
            if item is None:
                continue
            if since and item.occurred_at < str(since):
                continue
            latest = max(latest, item.occurred_at)
            yield item

        if latest:
            self.cursor = latest

    def _to_item(self, event: dict[str, str]) -> SourceItem | None:
        uid = event.get("UID", "")
        match = ASSIGNMENT_UID.search(uid)
        if match is None:
            return None  # a class meeting, not an obligation
        due = _parse_dt(event.get("DTSTART", ""))
        if not due:
            return None  # no date, no commitment — canvas.py's rule, same reason

        title = _unescape(event.get("SUMMARY", "")).strip() or "Assignment"
        # Canvas puts the course in brackets at the end of the summary: "Problem set 3
        # [BIO 181]". Split rather than parsed out of the description, which is rubric
        # boilerplate the API connector deliberately refuses to pay to read.
        course = ""
        if title.endswith("]") and "[" in title:
            title, _, tail = title.rpartition("[")
            course = tail.rstrip("]").strip()
            title = title.strip()

        verdict = self.boundary.check([course] if course else [])
        if not verdict.allowed:
            self.excluded += 1
            rule = verdict.matched_rule or "?"
            self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + 1
            return None

        author = course or "Canvas"
        heading = f"{course} — {title}" if course else title
        body = f"{author}: {title} is due {due}."
        return SourceItem(
            source=self.name,
            external_id=f"assignment:{match.group(1)}",
            occurred_at=due,
            author=author,
            title=heading,
            body_text=body,
            raw_json=json.dumps(
                {
                    "assignment_id": match.group(1),
                    "due_at": due,
                    "course": course,
                    "html_url": event.get("URL", ""),
                    # Named in the row itself, not only in this module's docstring: a
                    # reader asking why a submitted assignment is still open should find
                    # the answer on the item rather than in the source tree.
                    "submission_state": "unknown — ICS feed carries none",
                },
                sort_keys=True,
            ),
            content_hash=content_hash(
                author=author, title=heading, body_text=body, occurred_at=due
            ),
        )

    def _redact(self, message: str) -> str:
        """The feed URL out of anything about to be stored or displayed.

        `base.safe_error` cannot do this one. It strips secrets by name (`token=…`) and by
        shape (a long opaque string), and this credential is neither — it is an ordinary
        https URL whose *path* is the secret, `/feeds/calendars/user_abc123.ics`. Anyone
        holding it reads the owner's coursework without logging in, so it is a bearer
        credential wearing a URL, and a `URLError` renders it in full.

        Found by the test rather than reasoned about: docs/08 says tokens never appear
        outside the credential table, and this one was about to.
        """
        url = self.feed_url.strip()
        if not url:
            return message
        cleaned = message.replace(url, "<canvas feed url>")
        return cleaned.replace(re.sub(r"^webcal://", "https://", url), "<canvas feed url>")

    def _get(self) -> str:
        # webcal:// is the same document over https; Calendar.app rewrites it silently and
        # urllib does not, so a URL copied straight out of Canvas would otherwise fail with
        # "unknown url type".
        url = re.sub(r"^webcal://", "https://", self.feed_url.strip())
        request = urllib.request.Request(url, headers={"Accept": "text/calendar"})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return str(response.read().decode("utf-8", errors="replace"))
