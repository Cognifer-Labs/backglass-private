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
the owner's 200 `calendar:asu` rows are the proof: every one dropped at tier 0. Read
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
`event-calendar-event-<id>`. There is a third prefix and missing it cost a course:
`event-assignment-override-<id>` is an assignment whose due date belongs to the owner's
section, published *instead of* the base event rather than beside it, so a parser that
does not know the shape does not lose a date — it loses the assignment. See
`OVERRIDE_UID`. Only assignments are obligations, so only they are emitted;
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
from datetime import UTC, datetime, timedelta

from backglass.connectors.base import Cursor, Health, SourceItem, content_hash, safe_error
from backglass.connectors.boundary import Boundary
from backglass.retraction import RetractableWindow

#: Canvas's own discriminator. Everything else in the feed is a calendar event, which is
#: `apple_calendar`'s job and not an obligation.
ASSIGNMENT_UID = re.compile(r"event-assignment-(\d+)")

#: `event-assignment-override-916149` — an assignment whose due date belongs to the
#: owner's own section rather than to the course. Canvas emits it *instead of* the base
#: VEVENT, not beside it, so failing to recognise the shape does not lose a date: it
#: loses the assignment.
#:
#: Which is what happened. `ASSIGNMENT_UID` needs a digit straight after
#: `event-assignment-`, `override-` is not one, and so every section-dated assignment
#: fell out of the feed silently — twelve of them on 2026-08-27, ten of which were the
#: whole of CHM 113 Laboratory's graded work for September. Nothing failed: the connector
#: reported `ok`, the count it published was the count it parsed, and the ledger's own
#: audit could only ever say that every assignment it held had a commitment behind it.
#: The feed had 219 assignments and the ledger had 207, and no surface compared them.
OVERRIDE_UID = re.compile(r"event-assignment-override-(\d+)")

#: The assignment's own id, out of the VEVENT's URL — `…#assignment_7494004`. An override
#: id identifies the *override*, so keying on it would make one assignment two rows the
#: day its section date changed. The URL is where the stable id survives.
URL_ASSIGNMENT = re.compile(r"assignment_(\d+)")

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


@dataclass(frozen=True)
class ParsedAssignment:
    """One assignment as the feed currently publishes it.

    Everything the VEVENT holds, not the one sentence the `SourceItem` can carry. The item
    is immutable and cannot be widened after the fact — `ledger.upsert_source_item` treats
    a differing `content_hash` on a stored `external_id` as a conflict and skips it — so
    the description, the URL and the current due date reach `coursework` on this object
    instead. See migration 0031 for why that is not a workaround.
    """

    assignment_id: str
    external_id: str
    course: str
    title: str
    due_at: str
    url: str
    description: str


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

    #: Every assignment id the *document* contained on the last read, recorded before the
    #: boundary check and before anything is emitted. That distinction is the whole safety
    #: property of `retraction.reconcile`: an assignment the boundary excludes was still
    #: returned by the store, and recording only what was emitted is precisely the
    #: near-miss retraction.py documents — it came one clean run from deleting sixteen
    #: live classes.
    seen_ids: set[str] = field(default_factory=set)
    #: False until a read parses the whole document without raising. A partial or failed
    #: read must certify nothing at all: "the feed returned nothing" and "the feed no
    #: longer has anything" are the same bytes to everything downstream.
    read_complete: bool = False

    #: Every assignment this fetch parsed, in feed order. Read by `sync` after the loop
    #: through `getattr`, the same seam `seen_chats`, `excluded_by_rule` and
    #: `failed_calendars` already use: a connector emits SourceItems and nothing else, and
    #: anything richer is an attribute the caller may or may not know about. One fetch
    #: fills both, so the assignment record and the ledger row can never disagree about
    #: what the document said.
    assignments: list[ParsedAssignment] = field(default_factory=list)

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
        """Every assignment the feed publishes. `since` is deliberately not a filter.

        This read *used* to watermark on the latest due date, and that silently cost the
        owner a semester. A due date is not monotonic with publication: the feed is one
        document that gains assignments whose due dates fall *before* the furthest date
        already seen. The first read on 2026-08-11 stored four items and parked the cursor
        at 2026-09-04; the read on 08-17 saw a course publish "Excused Absence Requests"
        due 2027-03-07 and parked there; every read after that emitted nothing at all,
        because no real coursework is due after March. 157 assignments upstream, 6 in the
        ledger, and the credential row said `ok` the entire time.

        So there is nothing to bound. The document is fetched whole either way — there is
        no server-side filter to ask for — and emitting all of it costs zero writes on an
        unchanged assignment because `ledger.record` short-circuits on `content_hash`
        before any model is invoked. The watermark bought nothing and dropped everything.

        The cursor is still advanced, to the instant of the read rather than to a due
        date: it says when the feed was last successfully read, which is the only
        monotonic fact here, and nothing reads it back as a bound. A timestamp cannot
        park ahead of the data.
        """
        self.excluded = 0
        self.excluded_by_rule = {}
        self.assignments = []
        self.seen_ids = set()
        self.read_complete = False

        for event in _events(self._get()):
            parsed = self._parse(event)
            if parsed is None:
                continue
            self.assignments.append(parsed)
            yield self._item_for(parsed)

        # Only here, after the generator has been driven to exhaustion without raising.
        # `_get()` failing, the HTTP layer timing out, or a caller abandoning the iterator
        # all leave this False, and `retractable_window` then certifies nothing.
        self.read_complete = True
        self.cursor = datetime.now(UTC).isoformat()

    def retractable_window(self) -> RetractableWindow | None:
        """What this read certifies, for `retraction.reconcile`. None when it certifies
        nothing.

        The Canvas feed is one document fetched whole with no server-side filter — see
        `fetch` — so a completed read has seen everything the store publishes. That is the
        `Reconcilable` contract, and it is why an assignment deleted or unpublished
        upstream can be retracted here while an incremental source could never say so.

        The window is the span of what the read actually returned, not "all of time".
        Bounding it that way is conservative in the safe direction: an assignment deleted
        at the very edge of the span shrinks the span past itself and is simply not
        retracted this run, which is a missed retraction rather than deleted history.

        `calendars=None` — a Canvas feed has no sub-stores, so this read is evidence about
        every row it covers.
        """
        if not self.read_complete or not self.seen_ids:
            return None
        dues = sorted(str(a.due_at) for a in self.assignments if a.due_at)
        if not dues:
            return None
        # Exclusive upper bound, so the latest assignment in the feed is inside its own
        # window. One second past the last due date rather than a day: anything further
        # out was never in this read's scope and absence says nothing about it.
        last = datetime.fromisoformat(dues[-1]) + timedelta(seconds=1)
        return RetractableWindow(
            starts_at=dues[0],
            ends_before=last.isoformat(),
            seen_ids=set(self.seen_ids),
        )

    def _parse(self, event: dict[str, str]) -> ParsedAssignment | None:
        """One VEVENT as the whole assignment, or None when it is not one of ours.

        Split out of `_to_item` because the `SourceItem` is the lossy half. The item
        carries one sentence by design — docs/02's cost model, and a Canvas description is
        often boilerplate — but the description is also where the tools, the chapter
        ranges and the page counts live, and `coursework` needs those to say how long the
        work takes. Parsing once and rendering twice keeps the two from drifting.
        """
        uid = event.get("UID", "")
        override = OVERRIDE_UID.search(uid)
        if override is not None:
            # A section-dated assignment. Keyed by the assignment it is a date for, so it
            # occupies the same row whichever shape the feed publishes it in next; the
            # override id is the fallback only when Canvas emits no linkable URL, and an
            # ingested row under an odd id is still recoverable in a way a dropped one is
            # not.
            found = URL_ASSIGNMENT.search(event.get("URL", ""))
            assignment_id = found.group(1) if found else f"override-{override.group(1)}"
        else:
            match = ASSIGNMENT_UID.search(uid)
            if match is None:
                return None  # a class meeting, not an obligation
            assignment_id = match.group(1)
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

        # Recorded before the boundary check, deliberately: this is what the *store*
        # returned. An assignment the boundary excludes is still an assignment Canvas
        # published, and leaving it out here would make the next run read it as deleted.
        self.seen_ids.add(f"assignment:{assignment_id}")

        verdict = self.boundary.check([course] if course else [])
        if not verdict.allowed:
            self.excluded += 1
            rule = verdict.matched_rule or "?"
            self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + 1
            return None

        # DESCRIPTION is the plain-text half of the pair Canvas emits; X-ALT-DESC is the
        # same content as HTML. Taking the plain one is not laziness — it is already
        # unescaped prose with its links rendered as `[label] (url)`, so nothing here has
        # to strip tags, and a tag-stripper is a thing that silently eats content.
        description = _unescape(event.get("DESCRIPTION", "")).strip()
        return ParsedAssignment(
            assignment_id=assignment_id,
            external_id=f"assignment:{assignment_id}",
            course=course,
            title=title,
            due_at=due,
            url=event.get("URL", ""),
            description=description,
        )

    def _to_item(self, event: dict[str, str]) -> SourceItem | None:
        parsed = self._parse(event)
        return None if parsed is None else self._item_for(parsed)

    def _item_for(self, parsed: ParsedAssignment) -> SourceItem:
        course, title, due = parsed.course, parsed.title, parsed.due_at
        author = course or "Canvas"
        heading = f"{course} — {title}" if course else title
        body = f"{author}: {title} is due {due}."
        return SourceItem(
            source=self.name,
            external_id=parsed.external_id,
            occurred_at=due,
            author=author,
            title=heading,
            body_text=body,
            # `content_hash` covers (author, title, body_text, occurred_at) and not this,
            # so widening the raw record costs no conflict on the 159 items already
            # stored — they keep the terse shape they were written with, and everything
            # from here on carries the document. `body_text` deliberately stays as it was:
            # putting the description there would rewrite every stored hash, and an
            # immutable item answers that with a run error, not an update.
            raw_json=json.dumps(
                {
                    "assignment_id": parsed.assignment_id,
                    "due_at": due,
                    "course": course,
                    "html_url": parsed.url,
                    "description": parsed.description,
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
