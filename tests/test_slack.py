"""Slack. Discovery plus consent, a cursor that is a map rather than a watermark, and the
`{"ok": false}` envelope that carries every failure Slack has.

The 2026-08-24 ruling moved this connector from "reads the IDs in `.env`" to "enumerates
what the owner is a member of and reads what the owner chose on /chats". Several tests here
pin the *new* contract where an older one pinned the opposite; each says so where it matters,
because a test that quietly reverses is indistinguishable from a test that broke.

docs/10 §Testing: no test calls a live API. The transport is injected.
"""

from __future__ import annotations

import json
import time
import urllib.error
from typing import Any

import pytest

from backglass.connectors.allowlist import Allowlist
from backglass.connectors.base import Connector
from backglass.connectors.boundary import Boundary
from backglass.connectors.slack import SlackConnector, Watermarks

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
    """Serves canned `conversations.history` pages, in order, per channel — canned
    `conversations.replies` pages per (channel, parent ts) thread, and a canned
    `users.conversations` / `users.list` directory.

    Pages are consumed sequentially: the connector's `cursor` param selects the index, so a
    connector that ignores `next_cursor` visibly stops at page one.
    """

    def __init__(
        self,
        pages: dict[str, list[dict[str, Any]]],
        *,
        threads: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
        auth: dict[str, Any] | None = None,
        conversations: list[dict[str, Any]] | None = None,
        users: list[dict[str, Any]] | None = None,
    ):
        self.pages = pages
        self.threads = threads or {}
        self.auth = auth or {"ok": True, "user_id": "U0KAY", "team": "Backglass"}
        self.conversations = conversations if conversations is not None else []
        self.users = users if users is not None else []
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, method: str, params: dict[str, str]) -> dict[str, Any]:
        self.calls.append((method, dict(params)))
        if method == "auth.test":
            return self.auth
        if method == "users.conversations":
            return {"ok": True, "channels": self.conversations}
        if method == "users.list":
            return {"ok": True, "members": self.users}
        channel = params["channel"]
        index = int(params.get("cursor") or 0)
        if method == "conversations.replies":
            pages = self.threads.get((channel, params["ts"]), [])
        else:
            pages = self.pages.get(channel, [])
        if index >= len(pages):
            return a_page([])
        return pages[index]

    def history_params(self, channel: str) -> list[dict[str, str]]:
        return self._params("conversations.history", channel)

    def replies_params(self, channel: str) -> list[dict[str, str]]:
        return self._params("conversations.replies", channel)

    def _params(self, method: str, channel: str) -> list[dict[str, str]]:
        return [
            params
            for called, params in self.calls
            if called == method and params.get("channel") == channel
        ]


def a_connector(transport: FakeSlack, boundary: Boundary, *channels: str) -> SlackConnector:
    """A connector configured the legacy way: explicit IDs, no allowlist.

    `channel_ids` is still an explicit instruction and still read — see
    `test_an_env_listed_channel_is_read_even_when_discovery_fails`.
    """
    return SlackConnector(
        token="xoxb-test",
        channel_ids=channels or (CHANNEL,),
        boundary=boundary,
        transport=transport,
    )


def a_conversation(channel_id: str, **kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"id": channel_id, "name": "founders", "num_members": 4}
    base.update(kwargs)
    return base


def cursor_of(connector: SlackConnector) -> dict[str, Any]:
    return json.loads(str(connector.cursor))


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
    assert item.author == "U0DANA", "no directory entry to resolve, so the raw id stands"
    assert item.title == f"#{CHANNEL}"
    assert item.body_text == "I'll send the revised scope by Friday."
    assert json.loads(item.raw_json)["ts"] == "1753800000.000200"
    assert json.loads(item.raw_json)["channel"] == CHANNEL
    assert item.content_hash


def test_names_are_resolved_for_display_and_never_for_the_hash(enforcing: Boundary) -> None:
    """The canvas:ics failure mode, prevented by construction.

    A display name can drift — a rename upstream, or a `users.list` that fails once — and a
    hash over a drifting field recomputes differently for a row the 0002 trigger has
    already frozen. So the readable form goes in the fields and the stable IDs go in the
    hash, and the two runs below must agree on the hash while disagreeing on the name.
    """
    pages = {CHANNEL: [a_page([a_message()])]}
    directory = dict(
        conversations=[a_conversation(CHANNEL)],
        users=[{"id": "U0DANA", "profile": {"display_name": "Dana Ruiz"}}],
    )

    named = SlackConnector(
        token="t",
        boundary=enforcing,
        allowlist=Allowlist([CHANNEL]),
        transport=FakeSlack(pages, **directory),
    )
    (readable,) = list(named.fetch(None))
    assert readable.title == "#founders"
    assert readable.author == "Dana Ruiz"

    # Same messages, directory unavailable. Falls back to raw ids...
    bare = SlackConnector(
        token="t", boundary=enforcing, channel_ids=(CHANNEL,), transport=FakeSlack(pages)
    )
    (raw,) = list(bare.fetch(None))
    assert (raw.title, raw.author) == (f"#{CHANNEL}", "U0DANA")

    # ...and the ledger sees no change at all, which is the point.
    assert raw.content_hash == readable.content_hash
    assert raw.external_id == readable.external_id


def test_bot_and_noise_subtypes_are_skipped(enforcing: Boundary) -> None:
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


@pytest.mark.parametrize("subtype", ["thread_broadcast", "file_share", "me_message"])
def test_a_human_subtype_survives_the_filter(enforcing: Boundary, subtype: str) -> None:
    """The old blanket `if message.get("subtype")` dropped these.

    `thread_broadcast` is a reply the sender chose to send to the channel as well, and
    `file_share` carries the sender's own words alongside the file — both are exactly where
    a commitment gets made, and both were invisible.
    """
    transport = FakeSlack(
        {CHANNEL: [a_page([a_message(subtype=subtype, text="Yes — I'll own that by Friday.")])]}
    )
    items = list(a_connector(transport, enforcing).fetch(None))
    assert [item.body_text for item in items] == ["Yes — I'll own that by Friday."]


def test_the_boundary_runs_before_persistence(enforcing: Boundary) -> None:
    """docs/08 D1. A pasted client thread is client correspondence in Slack too."""
    transport = FakeSlack(
        {CHANNEL: [a_page([a_message(text="fwd from dana@clientexample.gov re: WIC")])]}
    )
    connector = a_connector(transport, enforcing)
    assert list(connector.fetch(None)) == []
    assert connector.excluded == 1
    assert connector.excluded_by_rule == {"clientexample.gov": 1}


# ── discovery and consent ─────────────────────────────────────────────────


def test_discovery_records_every_conversation_and_reads_only_the_chosen_one(
    enforcing: Boundary,
) -> None:
    """The 2026-08-24 ruling. Enumeration is metadata; reading needs a yes.

    This reverses `test_the_connector_reads_only_the_channels_it_was_given`, which asserted
    "no conversations.list, ever" — see the module docstring.
    """
    transport = FakeSlack(
        {CHANNEL: [a_page([a_message()])], OTHER: [a_page([a_message(text="secret")])]},
        conversations=[
            a_conversation(CHANNEL),
            a_conversation(OTHER, name="", is_im=True, user="U0DANA", num_members=None),
        ],
        users=[{"id": "U0DANA", "profile": {"display_name": "Dana Ruiz"}}],
    )
    connector = SlackConnector(
        token="t", boundary=enforcing, allowlist=Allowlist([CHANNEL]), transport=transport
    )
    items = list(connector.fetch(None))

    assert set(connector.seen_chats) == {CHANNEL, OTHER}, "both are offered on /chats"
    assert connector.seen_chats[OTHER].display_name == "@Dana Ruiz"
    assert connector.seen_chats[OTHER].kind == "direct"
    assert connector.seen_chats[CHANNEL].display_name == "#founders"
    assert connector.seen_chats[CHANNEL].participants == 4

    assert [item.body_text for item in items] == ["I'll send the revised scope by Friday."]
    assert transport.history_params(OTHER) == [], "an undecided conversation is never read"


def test_discovery_runs_even_when_nothing_has_been_chosen(enforcing: Boundary) -> None:
    """tasks/lessons.md 2026-08-03: a page whose input comes from the thing it disables
    stays blank forever. An empty allowlist is exactly when discovery matters."""
    transport = FakeSlack(
        {CHANNEL: [a_page([a_message()])]}, conversations=[a_conversation(CHANNEL)]
    )
    connector = SlackConnector(token="t", boundary=enforcing, transport=transport)

    assert list(connector.fetch(None)) == [], "nothing chosen, so nothing read"
    assert set(connector.seen_chats) == {CHANNEL}, "and yet the choice is now offerable"
    assert transport.history_params(CHANNEL) == []


def test_an_undecided_conversation_is_counted_at_zero_not_guessed(enforcing: Boundary) -> None:
    """Counting a conversation would mean reading it, which is the thing consent gates."""
    transport = FakeSlack(
        {CHANNEL: [a_page([a_message(), a_message(ts="1753800000.000300", text="two")])]},
        conversations=[a_conversation(CHANNEL), a_conversation(OTHER, name="ops")],
    )
    connector = SlackConnector(
        token="t", boundary=enforcing, allowlist=Allowlist([CHANNEL]), transport=transport
    )
    list(connector.fetch(None))
    assert connector.seen_chats[CHANNEL].messages == 2
    assert connector.seen_chats[OTHER].messages == 0


def test_an_env_listed_channel_is_read_even_when_discovery_fails(enforcing: Boundary) -> None:
    """Rule 5, applied to enumeration: `SLACK_CHANNELS` is an explicit instruction and does
    not depend on a call that can fail."""

    class NoDirectory(FakeSlack):
        def __call__(self, method: str, params: dict[str, str]) -> dict[str, Any]:
            if method == "users.conversations":
                self.calls.append((method, dict(params)))
                return {"ok": False, "error": "missing_scope"}
            return super().__call__(method, params)

    transport = NoDirectory({CHANNEL: [a_page([a_message()])]})
    connector = a_connector(transport, enforcing, CHANNEL)
    assert len(list(connector.fetch(None))) == 1
    assert connector.seen_chats == {}


def test_the_decision_source_is_the_workspace_not_the_label(enforcing: Boundary) -> None:
    """`sync` writes sightings under `chats_source`. A conversation is monitored or not,
    whatever label the credential row carries."""
    connector = SlackConnector(token="t", boundary=enforcing, label="work")
    assert connector.name == "slack:work"
    assert connector.chats_source == "slack"


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


# ── thread replies ────────────────────────────────────────────────────────

PARENT_TS = "1753800000.000200"


def a_thread_page(
    replies: list[dict[str, Any]],
    *,
    parent: dict[str, Any] | None = None,
    next_cursor: str = "",
) -> dict[str, Any]:
    """`conversations.replies` re-serves the parent as item one — docs/12 §4."""
    first = [parent] if parent is not None else []
    return a_page(first + replies, next_cursor=next_cursor)


def test_thread_replies_are_fetched_and_the_parent_is_not_duplicated(
    enforcing: Boundary,
) -> None:
    """docs/12 §4: conversations.history returns parents only, so "yes, Friday" said inside
    a thread is invisible without the fan-out."""
    parent = a_message(ts=PARENT_TS, text="Can you own the scope doc?", reply_count=2)
    transport = FakeSlack(
        {CHANNEL: [a_page([parent])]},
        threads={
            (CHANNEL, PARENT_TS): [
                a_thread_page(
                    [
                        a_message(ts="1753800100.000100", text="Yes — Friday."),
                        a_message(ts="1753800200.000300", user="U0KAY", text="Thanks."),
                    ],
                    parent=parent,
                )
            ]
        },
    )
    connector = a_connector(transport, enforcing)
    items = list(connector.fetch(None))

    assert [item.body_text for item in items] == [
        "Can you own the scope doc?",
        "Yes — Friday.",
        "Thanks.",
    ]
    assert [item.external_id for item in items] == [
        f"{CHANNEL}:{PARENT_TS}",
        f"{CHANNEL}:1753800100.000100",
        f"{CHANNEL}:1753800200.000300",
    ]
    assert len(set(item.external_id for item in items)) == 3, "the parent copy is dropped"

    call = transport.replies_params(CHANNEL)[0]
    assert call["ts"] == PARENT_TS
    assert call["limit"] == "200"


def test_replies_are_paginated_and_filtered_like_parents(enforcing: Boundary) -> None:
    parent = a_message(ts=PARENT_TS, text="parent", reply_count=3)
    transport = FakeSlack(
        {CHANNEL: [a_page([parent])]},
        threads={
            (CHANNEL, PARENT_TS): [
                a_thread_page(
                    [a_message(ts="1753800100.000100", bot_id="B0BOT", text="Deploy ok")],
                    parent=parent,
                    next_cursor="1",
                ),
                a_thread_page(
                    [
                        a_message(
                            ts="1753800100.000200", subtype="channel_join", text="joined"
                        ),
                        a_message(ts="1753800100.000300", text="the real answer"),
                    ]
                ),
            ]
        },
    )
    items = list(a_connector(transport, enforcing).fetch(None))
    assert [item.body_text for item in items] == ["parent", "the real answer"]
    assert len(transport.replies_params(CHANNEL)) == 2


def test_a_parent_with_no_replies_costs_no_request(enforcing: Boundary) -> None:
    """The rate limit is 1 request/minute for new apps (docs/12 §4); a reply_count of zero
    must not spend one."""
    transport = FakeSlack({CHANNEL: [a_page([a_message(reply_count=0)])]})
    assert len(list(a_connector(transport, enforcing).fetch(None))) == 1
    assert transport.replies_params(CHANNEL) == []


def test_a_late_reply_to_a_pre_cursor_thread_is_found(enforcing: Boundary) -> None:
    """The gap this module used to document as accepted, closed.

    The parent sits below the channel watermark and its `ts` does not move when a reply
    lands, so `conversations.history` will never re-serve it. Without the thread map, the
    reply below is unreachable forever.
    """
    # Inside the thread window on purpose: pruning is a separate rule with its own test,
    # and a fixture dated last year would drop the thread before the point is made.
    started = f"{time.time() - 3 * 86400:.6f}"
    parent = a_message(ts=started, text="Can you own the scope doc?", reply_count=1)
    first_transport = FakeSlack(
        {CHANNEL: [a_page([parent])]},
        threads={(CHANNEL, started): [a_thread_page([], parent=parent)]},
    )
    first = a_connector(first_transport, enforcing)
    list(first.fetch(None))
    cursor = first.cursor
    assert json.loads(str(cursor))["threads"] == {f"{CHANNEL}:{started}": started}

    # Run two: history has nothing new at all — the parent is below the watermark.
    landed = f"{time.time() - 60:.6f}"
    late = a_message(ts=landed, text="Yes — Friday.")
    second_transport = FakeSlack(
        {CHANNEL: [a_page([])]},
        threads={(CHANNEL, started): [a_thread_page([late], parent=parent)]},
    )
    second = a_connector(second_transport, enforcing)
    items = list(second.fetch(cursor))

    assert [item.body_text for item in items] == ["Yes — Friday."]
    assert cursor_of(second)["threads"] == {f"{CHANNEL}:{started}": landed}
    assert second_transport.replies_params(CHANNEL)[0]["oldest"] == started


def test_a_quiet_thread_leaves_the_map_and_stops_costing_a_request(
    enforcing: Boundary,
) -> None:
    """The bound on pass two. Each tracked thread is one request per run, forever, unless
    something forgets it."""
    old = str(time.time() - 90 * 86400)
    marks = Watermarks(threads={f"{CHANNEL}:{old}": old, f"{CHANNEL}:{PARENT_TS}": PARENT_TS})
    connector = SlackConnector(
        token="t",
        boundary=enforcing,
        channel_ids=(CHANNEL,),
        thread_window_days=30,
        transport=FakeSlack({CHANNEL: [a_page([])]}),
    )
    list(connector.fetch(marks.dumps()))
    assert cursor_of(connector)["threads"] == {}, "both are older than the window"


def test_the_repoll_budget_is_reported_rather_than_silently_capped(
    enforcing: Boundary,
) -> None:
    recent = time.time() - 3600
    marks = Watermarks(
        threads={f"{CHANNEL}:{recent + n:.6f}": f"{recent + n:.6f}" for n in range(30)}
    )
    transport = FakeSlack({CHANNEL: [a_page([])]})
    connector = SlackConnector(
        token="t", boundary=enforcing, channel_ids=(CHANNEL,), transport=transport
    )
    list(connector.fetch(marks.dumps()))

    assert len(transport.replies_params(CHANNEL)) == 25
    assert connector.threads_deferred == 5


def test_the_deferred_count_reaches_the_run_report(capsys: pytest.CaptureFixture[str]) -> None:
    """A cap nothing reads is a cap nobody knows about.

    `threads_deferred` sat on the connector and no surface read it, so a run that skipped
    two hundred threads and a run with none to skip printed the same thing. It rides the
    same attribute seam as `excluded_by_rule`, and this is the end of that seam.
    """
    from backglass.__main__ import _print_report
    from backglass.sync import SyncReport

    _print_report(
        SyncReport(deferred_work={"slack:personal: threads": 5}), dry_run=False
    )
    out = capsys.readouterr().out
    assert "deferred 5 slack:personal: threads to the next run" in out

    _print_report(SyncReport(), dry_run=False)
    assert "deferred" not in capsys.readouterr().out, "silent when there is nothing to say"


def test_ratelimited_during_the_replies_fan_out_keeps_what_was_read(
    enforcing: Boundary,
) -> None:
    class ThrottledThreads(FakeSlack):
        def __call__(self, method: str, params: dict[str, str]) -> dict[str, Any]:
            self.calls.append((method, dict(params)))
            if method == "conversations.replies":
                return {"ok": False, "error": "ratelimited"}
            return super().__call__(method, params)

    transport = ThrottledThreads(
        {CHANNEL: [a_page([a_message(ts="1753890000.000300", reply_count=1)])]}
    )
    connector = a_connector(transport, enforcing)

    items = list(connector.fetch("1753800000.000200"))  # no raise
    assert len(items) == 1, "the parent already yielded stays yielded"
    assert connector.rate_limited is True
    # The channel did not finish, so its watermark does not move past the interrupted
    # fan-out — the parent is re-served next run, which content_hash makes free.
    assert cursor_of(connector)["channels"] == {}
    assert cursor_of(connector)["floor"] == "1753800000.000200"


def test_replies_are_bounded_by_the_threads_own_watermark(enforcing: Boundary) -> None:
    cursor = "1753800000.000200"
    parent = a_message(ts="1753800500.000100", reply_count=1)
    transport = FakeSlack(
        {CHANNEL: [a_page([parent])]},
        threads={
            (CHANNEL, "1753800500.000100"): [
                a_thread_page([a_message(ts="1753800600.000100", text="reply")], parent=parent)
            ]
        },
    )
    list(a_connector(transport, enforcing).fetch(cursor))
    # A thread never polled before starts at the channel's position, which here is the
    # legacy floor.
    assert transport.replies_params(CHANNEL)[0]["oldest"] == cursor


# ── the cursor ────────────────────────────────────────────────────────────


def test_each_channel_carries_its_own_watermark(enforcing: Boundary) -> None:
    transport = FakeSlack(
        {
            CHANNEL: [a_page([a_message(ts="1753800000.000200")])],
            OTHER: [a_page([a_message(ts="1753890000.000300", text="later, elsewhere")])],
        }
    )
    connector = a_connector(transport, enforcing, CHANNEL, OTHER)
    assert len(list(connector.fetch(None))) == 2
    assert cursor_of(connector)["channels"] == {
        CHANNEL: "1753800000.000200",
        OTHER: "1753890000.000300",
    }


def test_a_finished_channel_keeps_its_progress_when_a_later_one_is_throttled(
    enforcing: Boundary,
) -> None:
    """The defect this replaced, stated as a test.

    One watermark for the workspace only advanced after a clean pass over every channel,
    and a rate limit reset it to the start. Slack grants new apps about one history
    request a minute, so the clean pass never happened and the watermark never moved:
    every run re-read from zero, forever.
    """

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
    assert cursor_of(connector)["channels"] == {CHANNEL: "1753890000.000300"}
    assert OTHER not in cursor_of(connector)["channels"], "the throttled one is untouched"


def test_the_cursor_keeps_the_full_fractional_ts(enforcing: Boundary) -> None:
    """tasks/lessons.md, 2026-07-30: a watermark truncated downward re-reads everything on
    every run, and content_hash hides it by making the re-read produce zero writes."""
    transport = FakeSlack({CHANNEL: [a_page([a_message(ts="1753800000.000200")])]})
    connector = a_connector(transport, enforcing)
    list(connector.fetch(None))
    assert cursor_of(connector)["channels"][CHANNEL] == "1753800000.000200"


def test_a_legacy_bare_ts_cursor_is_read_as_a_floor(enforcing: Boundary) -> None:
    """The only migration a cursor gets. An install holding the old single watermark must
    not re-read its whole workspace, and must not skip a channel it never reached."""
    transport = FakeSlack(
        {
            CHANNEL: [
                a_page(
                    [
                        a_message(ts="1753800000.000100", text="below"),
                        a_message(ts="1753890000.000300", text="above"),
                    ]
                )
            ]
        }
    )
    connector = a_connector(transport, enforcing)
    items = list(connector.fetch("1753800000.000200"))

    assert [item.body_text for item in items] == ["above"]
    assert transport.history_params(CHANNEL)[0]["oldest"] == "1753800000.000200"
    assert cursor_of(connector)["floor"] == "1753800000.000200"


def test_an_unparseable_cursor_starts_over_rather_than_raising(enforcing: Boundary) -> None:
    transport = FakeSlack({CHANNEL: [a_page([a_message()])]})
    connector = a_connector(transport, enforcing)
    assert len(list(connector.fetch("{not json"))) == 1


def test_the_second_run_passes_each_channels_cursor_and_fetches_nothing(
    enforcing: Boundary,
) -> None:
    pages = {
        CHANNEL: [a_page([a_message(ts="1753800000.000200")])],
        OTHER: [a_page([a_message(ts="1753890000.000300", text="later, elsewhere")])],
    }
    first = a_connector(FakeSlack(pages), enforcing, CHANNEL, OTHER)
    list(first.fetch(None))
    cursor = first.cursor

    transport = FakeSlack(pages)
    second = a_connector(transport, enforcing, CHANNEL, OTHER)
    assert list(second.fetch(cursor)) == [], "nothing newer than the watermark"

    assert transport.history_params(CHANNEL)[0]["oldest"] == "1753800000.000200"
    assert transport.history_params(OTHER)[0]["oldest"] == "1753890000.000300"


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
    assert cursor_of(connector)["channels"][CHANNEL] == "1753803600.000100"


# ── rate limiting ─────────────────────────────────────────────────────────


class _Retrying:
    """A `urlopen` double: 429 with a `Retry-After`, then success."""

    def __init__(self, delays: list[str], payload: dict[str, Any] | None = None):
        self.delays = delays
        self.payload = payload if payload is not None else {"ok": True, "messages": []}
        self.opened = 0

    def __call__(self, request: Any, timeout: float | None = None) -> Any:
        del timeout
        self.opened += 1
        if self.delays:
            raise urllib.error.HTTPError(
                "https://slack.com/api/x",
                429,
                "Too Many Requests",
                {"Retry-After": self.delays.pop(0)},  # type: ignore[arg-type]
                None,
            )

        class _Response:
            def __init__(self, body: bytes) -> None:
                self.body = body

            def read(self) -> bytes:
                return self.body

            def __enter__(self) -> Any:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        return _Response(json.dumps(self.payload).encode())


def test_retry_after_is_obeyed_rather_than_thrown_away(
    enforcing: Boundary, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slack says how long to wait. The old code discarded the header and burned its
    retries against a wall whose height it had been told."""
    opener = _Retrying(["3"])
    slept: list[float] = []
    monkeypatch.setattr("backglass.connectors.slack.urllib.request.urlopen", opener)
    monkeypatch.setattr("backglass.connectors.slack.time.sleep", slept.append)

    connector = SlackConnector(token="t", boundary=enforcing, channel_ids=(CHANNEL,))
    assert connector._call("conversations.history", {"channel": CHANNEL}) == {
        "ok": True,
        "messages": [],
    }
    assert slept == [3.0]
    assert opener.opened == 2


def test_the_retry_budget_is_bounded_and_stops_the_run_cleanly(
    enforcing: Boundary, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scheduled sync that sleeps for an hour is a sync that never ran."""
    opener = _Retrying(["120", "120"])
    slept: list[float] = []
    monkeypatch.setattr("backglass.connectors.slack.urllib.request.urlopen", opener)
    monkeypatch.setattr("backglass.connectors.slack.time.sleep", slept.append)

    connector = SlackConnector(
        token="t",
        boundary=enforcing,
        channel_ids=(CHANNEL,),
        rate_limit_budget_seconds=150,
    )
    items = list(connector.fetch(None))

    assert items == []
    assert connector.rate_limited is True, "the second 120s would blow the 150s budget"
    assert slept == [120.0], "one wait spent, the next refused"


def test_an_absurd_retry_after_is_not_an_instruction(
    enforcing: Boundary, monkeypatch: pytest.MonkeyPatch
) -> None:
    opener = _Retrying(["99999"])
    slept: list[float] = []
    monkeypatch.setattr("backglass.connectors.slack.urllib.request.urlopen", opener)
    monkeypatch.setattr("backglass.connectors.slack.time.sleep", slept.append)

    connector = SlackConnector(token="t", boundary=enforcing, channel_ids=(CHANNEL,))
    assert list(connector.fetch(None)) == []
    assert connector.rate_limited is True
    assert slept == []


# ── the {"ok": false} envelope ────────────────────────────────────────────


def test_an_unknown_slack_error_is_raised_not_parsed_as_data(enforcing: Boundary) -> None:
    class Broken(FakeSlack):
        def __call__(self, method: str, params: dict[str, str]) -> dict[str, Any]:
            self.calls.append((method, dict(params)))
            if method == "users.conversations":
                return {"ok": True, "channels": []}
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


def test_a_missing_token_is_a_health_state_not_a_silent_zero(enforcing: Boundary) -> None:
    """docs/11 §8: reporting zero items when the source is misconfigured is the dangerous
    failure. The token is the only thing that can be *mis*configured now — an empty
    allowlist is a question waiting on /chats, not a fault."""
    health = SlackConnector(token="", boundary=enforcing).health()
    assert health.ok is False
    assert "SLACK_TOKEN" in str(health.detail)


def test_nothing_chosen_yet_is_healthy(enforcing: Boundary) -> None:
    connector = SlackConnector(token="t", boundary=enforcing, transport=FakeSlack({}))
    assert connector.health().ok is True


def test_slack_satisfies_the_connector_protocol(enforcing: Boundary) -> None:
    connector = SlackConnector(token="t", boundary=enforcing, channel_ids=(CHANNEL,))
    assert isinstance(connector, Connector)
