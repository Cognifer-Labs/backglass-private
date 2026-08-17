"""The connector interface from docs/07.

"Every connector implements the same interface and writes only `source_item` rows. None
of them know about commitments, goals, or plans."
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: An opaque per-connector position, stored in the credential row. Gmail uses a
#: historyId; Drive uses a changes-feed token. Nothing outside the connector reads it.
Cursor = str | None


@dataclass(frozen=True)
class Health:
    name: str
    ok: bool
    #: Present when ok is False. docs/07: auth expiry is a visible product state, not a
    #: log line, so this string ends up in the Sources panel and at the top of the brief.
    detail: str | None = None


@dataclass(frozen=True)
class SourceItem:
    """One immutable raw capture, ready to be written to `source_item`.

    `occurred_at` is when the thing happened, not when it was fetched. Every relative
    date resolves against it (docs/03). Getting this wrong is the most damaging bug this
    system can have, so it is required rather than nullable.
    """

    source: str
    external_id: str
    occurred_at: str
    content_hash: str
    author: str | None = None
    title: str | None = None
    body_text: str | None = None
    raw_json: str = "{}"


@dataclass(frozen=True)
class FetchResult:
    items: list[SourceItem]
    cursor: Cursor
    #: D5. Counted, never stored. docs/08: an excluded message is "not stored, not
    #: hashed, not counted beyond a tally".
    excluded: int = 0
    excluded_by_rule: dict[str, int] | None = None


@runtime_checkable
class Connector(Protocol):
    # A read-only property rather than the bare `name: str` attribute docs/07 sketches.
    # Gmail derives its name from the mailbox label ("gmail:personal"), and a settable
    # Protocol attribute cannot be satisfied by a derived property.
    @property
    def name(self) -> str: ...

    def health(self) -> Health: ...

    def fetch(self, since: Cursor) -> Iterator[SourceItem]: ...

    # `upstream_count` is optional; `getattr(connector, "upstream_count", None)` is how
    # callers ask for it, so no existing connector has to grow a stub that returns None.


@runtime_checkable
class Countable(Protocol):
    """A connector that can say how many items its store holds, cheaply and exactly.

    This exists because "is a source still working?" had no honest answer. A credential
    reading `ok` proves the last run did not raise; it says nothing about whether the
    run ingested anything, and neither does the age of the newest row. On 2026-08-16
    `apple-notes` had been silent for seventeen days and two separate checks called its
    cursor parked. The store held 65 notes, the newest modified 2026-07-05, the cursor
    sat at exactly that instant, and the owner had simply not written a note. A
    days-since threshold would have been wrong about the only case available to test it.

    The question a threshold is guessing at is answerable directly: **does the store
    hold items the ledger does not have?** 65 against 65 is health, stated as a fact and
    with nothing to tune. 65 against 40 is a cursor parked past its data, which is the
    real failure mode and is otherwise invisible.

    Implemented only where the connector enumerates a bounded local store and ingests all
    of it, because those are the conditions that make the comparison exact. A windowed or
    paged source (mail, a remote API) would need a query it cannot afford on every check
    and would reconcile to a number that means nothing, so it simply does not implement
    this and callers get None. Not on `Connector` for the same reason: a method three
    quarters of the connectors would have to stub is a method that teaches nobody
    anything.
    """

    @property
    def name(self) -> str: ...

    def upstream_count(self) -> int | None:
        """Items the store holds that this connector would ingest, or None if unknown.

        None is a real answer and must be reported as unknown rather than as zero — the
        distinction `backglass state` is built on.
        """
        ...


def content_hash(
    *, author: str | None, title: str | None, body_text: str | None, occurred_at: str
) -> str:
    """The change-detection primitive from docs/03.

    "Hash matches what is stored, item is skipped entirely and no model is invoked. This
    is what keeps a 30-minute cadence cheap."

    Computed over the *stripped* body, so a forty-message thread does not produce forty
    near-identical hashes (docs/07 §Gmail). Whitespace is normalised because Gmail
    re-wraps text bodies inconsistently between the raw and parsed representations, and a
    re-wrap is not a content change.
    """
    parts = [
        (author or "").strip().lower(),
        " ".join((title or "").split()),
        " ".join((body_text or "").split()),
        occurred_at.strip(),
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


#: Tokens whose *shape* is the giveaway, matched with no `key=` in front of them.
#: The name-prefixed pass below only fires on `token=…` and friends, and the messages
#: that actually leak a secret here are the ones that never name it: a Slack
#: `invalid_auth` echoing the token it rejected, a Canvas 401 quoting the URL path
#: segment, a JSON body reflected verbatim into the exception string. Every prefix is
#: the provider's own registered format, so a false positive is a string that was
#: already shaped exactly like a credential.
_BARE_SECRET = re.compile(
    r"xox[bp]-[A-Za-z0-9-]{8,}"  # Slack bot / user
    r"|ghp_[A-Za-z0-9]{20,}"  # GitHub personal access
    r"|github_pat_[A-Za-z0-9_]{20,}"  # GitHub fine-grained
    r"|sk-ant-[A-Za-z0-9_-]{8,}"  # Anthropic
    r"|\d{4,5}~[A-Za-z0-9]{40,}"  # Canvas
)


def safe_error(exc: Exception, *, limit: int = 300) -> str:
    """An exception rendered for storage and display, with credentials stripped.

    docs/08 §General handling: "Tokens never appear in logs, in the SQLite file outside
    the `credential` table, or in brief output." Google's client raises exceptions whose
    `str()` can carry the request URL, and on some transports that URL carries the access
    token as a query parameter — so the raw text of a failure is not safe to persist.

    Several connectors already had a private version of this for their `health()` path.
    It lives here now because the path that actually persists an error is the ingest
    loop in sync.py, which is shared: a per-connector redactor cannot protect a
    connector that never wrote one, and a new connector should not have to remember.
    """
    text = f"{type(exc).__name__}: {exc}"
    text = re.sub(r"(?i)(access_token|refresh_token|client_secret|api[_-]?key|key|token)"
                  r"\s*[=:]\s*[^&\s,)\"']+", r"\1=[redacted]", text)
    # Bearer headers and bare JWT-ish blobs, which carry no key= to match on.
    text = re.sub(r"(?i)\bBearer\s+[\w\-.~+/]+=*", "Bearer [redacted]", text)
    text = _BARE_SECRET.sub("[redacted]", text)
    return text[:limit]
