"""The Gmail connector: quote stripping, timestamps, hashing, and the cursor.

docs/07 §Gmail. Every test drives the connector through the fake service; none touch the
network.
"""

from __future__ import annotations

from backglass.connectors.base import content_hash
from backglass.connectors.gmail import occurred_at_of, strip_quoted
from tests.conftest import FakeGmailService, gmail_message, make_connector

THREAD_REPLY = """Sounds good, I'll send the scope Monday.

On Fri, Jul 10, 2026 at 9:15 AM Dana Whitfield <dwhitfield@example.gov> wrote:
> Can you get me the scope this week?
> Also, the migration plan is still pending.
>
>> Earlier still, from a third message.

--
Marcus Reed
Principal, Example Co
"""


def test_quoted_history_and_signature_are_stripped() -> None:
    stripped = strip_quoted(THREAD_REPLY)
    assert stripped == "Sounds good, I'll send the scope Monday."
    assert "Can you get me the scope" not in stripped
    assert "Principal, Example Co" not in stripped


def test_stripping_happens_before_hashing_so_a_thread_is_not_forty_items() -> None:
    """docs/07: "so a forty-message thread does not produce forty near-identical items".

    Two replies whose only difference is how much history they quote must hash the same
    once the quotes are gone — otherwise every reply is a fresh source_item and a fresh
    model call for text already read.
    """
    first = (
        "Sounds good, I'll send the scope Monday.\n\nOn Fri, Jul 10 2026, Dana wrote:\n> a\n"
    )
    second = (
        "Sounds good, I'll send the scope Monday.\n\n"
        "On Fri, Jul 10 2026, Dana wrote:\n> a\n> b\n> c\n"
    )

    hashes = {
        content_hash(
            author="m@example.com",
            title="Re: Scope",
            body_text=strip_quoted(body),
            occurred_at="2026-07-10T09:15:00-07:00",
        )
        for body in (first, second)
    }
    assert len(hashes) == 1


def test_occurred_at_keeps_the_senders_offset() -> None:
    """The offset is what makes CLAUDE.md rule 4 work across the owner's two timezones."""
    phoenix = occurred_at_of({"Date": "Fri, 10 Jul 2026 09:15:00 -0700"}, None)
    kolkata = occurred_at_of({"Date": "Sat, 11 Jul 2026 08:30:00 +0530"}, None)
    assert phoenix == "2026-07-10T09:15:00-07:00"
    assert kolkata == "2026-07-11T08:30:00+05:30"


def test_internal_date_is_only_a_fallback() -> None:
    """internalDate is UTC epoch milliseconds and has thrown the offset away."""
    assert occurred_at_of({}, "1752163200000").endswith("+00:00")


def test_the_connector_produces_one_item_per_message(boundary) -> None:  # type: ignore[no-untyped-def]
    spec = {
        "id": "m1",
        "from": "Marcus Reed <mreed@example.com>",
        "to": "contactdharsan@gmail.com",
        "subject": "Re: Scope",
        "date": "Fri, 10 Jul 2026 09:15:00 -0700",
        "body": THREAD_REPLY,
    }
    connector = make_connector([gmail_message(spec)], boundary)
    items = list(connector.fetch(None))

    assert len(items) == 1
    item = items[0]
    assert item.source == "gmail:personal"
    assert item.external_id == "m1"
    assert item.occurred_at == "2026-07-10T09:15:00-07:00"
    assert item.body_text == "Sounds good, I'll send the scope Monday."
    assert item.title == "Re: Scope"


def test_first_run_is_a_full_scan_and_records_a_cursor(boundary) -> None:  # type: ignore[no-untyped-def]
    """docs/07: "Full scan only on first run or cursor loss." """
    spec = {
        "id": "m1",
        "from": "a@example.com",
        "to": "b@example.com",
        "subject": "s",
        "date": "Fri, 10 Jul 2026 09:15:00 -0700",
        "body": "b",
    }
    connector = make_connector([gmail_message(spec)], boundary)
    list(connector.fetch(None))
    assert connector.cursor == "99001"


def test_an_expired_history_id_falls_back_to_a_full_scan(boundary) -> None:  # type: ignore[no-untyped-def]
    """Gmail expires historyIds after about a week. A laptop that slept through a holiday
    is the normal way to hit this, not an error — so it degrades to a full scan rather
    than failing the source."""
    spec = {
        "id": "m1",
        "from": "a@example.com",
        "to": "b@example.com",
        "subject": "s",
        "date": "Fri, 10 Jul 2026 09:15:00 -0700",
        "body": "b",
    }
    service = FakeGmailService([gmail_message(spec)])
    service.history_raises = True

    from backglass.connectors.gmail import GmailConnector

    connector = GmailConnector(label="personal", service=service, boundary=boundary)
    items = list(connector.fetch("stale-cursor"))
    assert len(items) == 1
    assert connector.cursor == "99001"


def test_health_reports_auth_failure_as_a_state_not_an_exception(boundary) -> None:  # type: ignore[no-untyped-def]
    """docs/07: "Auth expiry is a visible product state, not a log line." """
    service = FakeGmailService([])
    service.profile_raises = True
    from backglass.connectors.gmail import GmailConnector

    connector = GmailConnector(label="personal", service=service, boundary=boundary)
    health = connector.health()
    assert health.ok is False
    assert health.detail is not None and "invalid_grant" in health.detail


def test_errors_never_leak_a_token() -> None:
    """docs/08 §General handling: tokens never appear in logs."""
    from backglass.connectors.gmail import _safe_error

    leaked = _safe_error(RuntimeError("GET https://gmail/x?access_token=ya29.SECRET&alt=json"))
    assert "ya29.SECRET" not in leaked
    assert "[redacted]" in leaked
