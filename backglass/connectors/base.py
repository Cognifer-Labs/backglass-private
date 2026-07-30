"""The connector interface from docs/07.

"Every connector implements the same interface and writes only `source_item` rows. None
of them know about commitments, goals, or plans."
"""

from __future__ import annotations

import hashlib
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
