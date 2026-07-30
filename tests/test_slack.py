"""Slack. The connector reads only the channels the owner named, so these tests are about
the three things Slack does differently from every other source: a `{"ok": false}` envelope
instead of an HTTP status, a fractional-seconds `ts` that is both the ID and the clock, and
a message stream that is mostly bots and join notices.

docs/10 §Testing: no test calls a live API. The transport is injected.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from backglass.connectors.base import Connector
from backglass.connectors.boundary import Boundary
from backglass.connectors.slack import SlackConnector

CHANNEL = "C0FOUNDERS"
OTHER = "D0DIRECT"


@pytest.fixture
def enforcing() -> Boundary:
    return Boundary(mode="exclude", deny_domains=["clientexample.gov"])


# ── the fake transport ────────────────────────────────────────────────────


def a_message(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "type": "message",
        "ts": kwargs.pop("ts", "1753800000.000200"),
        "user": kwargs.pop("user", "U0DANA"),
        "text": kwargs.pop("text", "I'll send the revised scope by Friday."),
    }
    base.update(kwargs)
    return base


def a_page(messages: list[dict[str, Any]], next_cursor: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": True, "messages": messages, "has_more": bool(next_cursor)}
    if next_cursor:
        payload["response_metadata"] = {"next_cursor": next_cursor}
    return payload


class FakeSlack:
    """Serves canned `conversations.history` pages, in order, per channel.

    Pages are consumed sequentially: the connector's `cursor` param selects the index, so a
    connector that ignores `next_cursor` visibly stops at page one.
    """

    def __init__(
        self,
        pages: dict[str, list[dict[str, Any]]],
        *,
        auth: dict[str, Any] | None = None,
    ):
        self.pages = pages
        self.auth = auth or {"ok": True, "user_id": "U0KAY", "team": "Backglass"}
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, method: str, params: dict[str, str]) -> dict[str, Any]:
        self.calls.append((method, dict(params)))
        if method == "auth.test":
            return self.auth
        channel = params["channel"]
        index = int(params.get("cursor") or 0)
        pages = self.pages.get(channel, [])
        if index >= len(pages):
            return a_page([])
        return pages[index]

    def history_params(self, channel: str) -> list[dict[str, str]]:
        return [
            params
            for method, params in self.calls
            if method == "conversations.history" and params.get("channel") == channel
        ]


def a_connector(transport: FakeSlack, boundary: Boundary, *channels: str) -> SlackConnector:
    return SlackConnector(
        token="xoxb-test",
        channel_ids=channels or (CHANNEL,),
        boundary=boundary,
        transport=transport,
    )


# ── mapping ───────────────────────────────────────────────────────────────


def test_a_message_becomes_a_source_item(enforcing: Boundary) -> None:
    transport = FakeSlack({CHANNEL: [a_page([a_message()])]})
    items = list(a_connector(transport, enforcing).fetch(None))

    assert len(items) == 1
    item = items[0]
    assert item.source == "slack:personal"
    assert item.external_id == f"{CHANNEL}:1753800000.000200"
    # ts is the clock as well as the ID. Resolved to UTC, full precision kept.
    assert item.occurred_at == "2025-07-29T14:40:00.000200+00:00"
    assert item.author == "U0DANA", "the raw Slack user id; no users.info round trip"
    assert item.title == f"#{CHANNEL}"
    assert item.body_text == "I'll send the revised scope by Friday."
    assert json.loads(item.raw_json)["ts"] == "1753800000.000200"
    assert item.content_hash


def test_the_connector_reads_only_the_channels_it_was_given(enforcing: Boundary) -> None:
    """The scope decision, asserted: no conversations.list, ever."""
    transport = FakeSlack({CHANNEL: [a_page([a_message()])], OTHER: [a_page([])]})
    list(a_connector(transport, enforcing, CHANNEL).fetch(None))

    methods = {method for method, _ in transport.calls}
    assert methods == {"conversations.history"}
    assert transport.history_params(OTHER) == [], "an unlisted channel is never touched"


def test_bot_and_subtype_messages_are_skipped(enforcing: Boundary) -> None:
    """Bots are Slack's no-reply@ layer, and a join notice is not a commitment."""
    transport = FakeSlack(
        {
            CHANNEL: [
                a_page(
                    [
                        a_message(ts="1753800000.000100", bot_id="B0DEPLOY", text="Deploy ok"),
                        a_message(
                            ts="1753800000.000200",
                            subtype="channel_join",
                            text="<@U0DANA> has joined",
                        ),
                        a_message(ts="1753800000.000300", text="   "),
                        a_message(ts="1753800000.000400", text="Real one."),
                    ]
                )
            ]
        }
    )
    items = list(a_connector(transport, enforcing).fetch(None))
    assert [item.body_text for item in items] == ["Real one."]


def test_the_boundary_runs_before_persistence(enforcing: Boundary) -> None:
    """docs/08 D1. A pasted client thread is client correspondence in Slack too."""
    transport = FakeSlack(
        {CHANNEL: [a_page([a_message(text="fwd from dana@clientexample.gov re: WIC")])]}
    )
    connector = a_connector(transport, enforcing)
    assert list(connector.fetch(None)) == []
    assert connector.excluded == 1
    assert connector.excluded_by_rule == {"clientexample.gov": 1}


# ── pagination ────────────────────────────────────────────────────────────


def test_pagination_follows_next_cursor_to_completion(enforcing: Boundary) -> None:
    transport = FakeSlack(
        {
            CHANNEL: [
                a_page([a_message(ts="1753800000.000100", text="one")], next_cursor="1"),
                a_page([a_message(ts="1753800000.000200", text="two")], next_cursor="2"),
                a_page([a_message(ts="1753800000.000300", text="three")]),
            ]
        }
    )
    items = list(a_connector(transport, enforcing).fetch(None))
    assert [item.body_text for item in items] == ["one", "two", "three"]
    assert len(transport.history_params(CHANNEL)) == 3
    assert all(params["limit"] == "200" for params in transport.history_params(CHANNEL))


# ── cursor ────────────────────────────────────────────────────────────────


def test_the_cursor_is_the_highest_ts_across_every_channel(enforcing: Boundary) -> None:
    transport = FakeSlack(
        {
            CHANNEL: [a_page([a_message(ts="1753800000.000200")])],
            OTHER: [a_page([a_message(ts="1753890000.000300", text="later, elsewhere")])],
        }
    )
    connector = a_connector(transport, enforcing, CHANNEL, OTHER)
    assert len(list(connector.fetch(None))) == 2
    assert connector.cursor == "1753890000.000300"


def test_the_cursor_keeps_the_full_fractional_ts(enforcing: Boundary) -> None:
    """tasks/lessons.md, 2026-07-30: a watermark truncated downward re-reads everything on
    every run, and content_hash hides it by making the re-read produce zero writes."""
    transport = FakeSlack({CHANNEL: [a_page([a_message(ts="1753800000.000200")])]})
    connector = a_connector(transport, enforcing)
    list(connector.fetch(None))
    assert connector.cursor == "1753800000.000200", "not rounded, not reformatted"


def test_the_second_run_passes_the_cursor_as_oldest_and_fetches_nothing(
    enforcing: Boundary,
) -> None:
    pages = {
        CHANNEL: [a_page([a_message(ts="1753800000.000200")])],
        OTHER: [a_page([a_message(ts="1753890000.000300", text="later, elsewhere")])],
    }
    first = a_connector(FakeSlack(pages), enforcing, CHANNEL, OTHER)
    list(first.fetch(None))
    cursor = first.cursor
    assert cursor == "1753890000.000300"

    transport = FakeSlack(pages)
    second = a_connector(transport, enforcing, CHANNEL, OTHER)
    assert list(second.fetch(cursor)) == [], "nothing newer than the watermark"

    for channel in (CHANNEL, OTHER):
        assert transport.history_params(channel)[0]["oldest"] == cursor


def test_a_message_at_or_below_the_cursor_is_dropped_client_side(enforcing: Boundary) -> None:
    """Slack's `oldest` bound is not reliably exclusive, so the connector re-checks."""
    transport = FakeSlack(
        {
            CHANNEL: [
                a_page(
                    [
                        a_message(ts="1753800000.000200", text="already seen"),
                        a_message(ts="1753803600.000100", text="new"),
                    ]
                )
            ]
        }
    )
    connector = a_connector(transport, enforcing)
    items = list(connector.fetch("1753800000.000200"))
    assert [item.body_text for item in items] == ["new"]
    assert connector.cursor == "1753803600.000100"


# ── the {"ok": false} envelope ────────────────────────────────────────────


def test_ratelimited_stops_cleanly_and_keeps_the_cursor_safe(enforcing: Boundary) -> None:
    """CLAUDE.md rule 5 in its gentlest form: a quota stop is not a failure. The cursor
    stays where the run started, so the unread tail is re-served next run — which
    content_hash makes free."""

    class Throttling(FakeSlack):
        def __call__(self, method: str, params: dict[str, str]) -> dict[str, Any]:
            self.calls.append((method, dict(params)))
            if params.get("channel") == OTHER:
                return {"ok": False, "error": "ratelimited"}
            return super().__call__(method, params)

    transport = Throttling({CHANNEL: [a_page([a_message(ts="1753890000.000300")])]})
    connector = a_connector(transport, enforcing, CHANNEL, OTHER)

    items = list(connector.fetch("1753800000.000200"))  # no raise
    assert len(items) == 1
    assert connector.rate_limited is True
    assert connector.cursor == "1753800000.000200", "not advanced past an incomplete run"


def test_an_unknown_slack_error_is_raised_not_parsed_as_data(enforcing: Boundary) -> None:
    class Broken(FakeSlack):
        def __call__(self, method: str, params: dict[str, str]) -> dict[str, Any]:
            self.calls.append((method, dict(params)))
            return {"ok": False, "error": "channel_not_found"}

    connector = a_connector(Broken({}), enforcing)
    with pytest.raises(Exception, match="channel_not_found"):
        list(connector.fetch(None))


def test_invalid_auth_is_a_health_state_with_slacks_own_word_for_it(
    enforcing: Boundary,
) -> None:
    transport = FakeSlack({}, auth={"ok": False, "error": "invalid_auth"})
    health = a_connector(transport, enforcing).health()
    assert health.ok is False
    assert "invalid_auth" in str(health.detail)


def test_a_revoked_token_says_so(enforcing: Boundary) -> None:
    transport = FakeSlack({}, auth={"ok": False, "error": "token_revoked"})
    health = a_connector(transport, enforcing).health()
    assert health.ok is False
    assert "token_revoked" in str(health.detail)


def test_a_healthy_token_is_healthy(enforcing: Boundary) -> None:
    transport = FakeSlack({CHANNEL: [a_page([])]})
    health = a_connector(transport, enforcing).health()
    assert health.ok is True
    assert transport.calls[0][0] == "auth.test"


def test_no_configured_channels_is_a_health_state_not_a_silent_zero(
    enforcing: Boundary,
) -> None:
    """docs/11 §8: reporting zero items when the source is misconfigured is the dangerous
    failure. An empty channel list can only ever return nothing."""
    connector = SlackConnector(token="xoxb-test", channel_ids=(), boundary=enforcing)
    health = connector.health()
    assert health.ok is False
    assert "channel" in str(health.detail)


def test_slack_satisfies_the_connector_protocol(enforcing: Boundary) -> None:
    connector = SlackConnector(token="t", channel_ids=(CHANNEL,), boundary=enforcing)
    assert isinstance(connector, Connector)
