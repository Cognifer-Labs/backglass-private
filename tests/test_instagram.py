"""Instagram connector, both lanes, and the friend-plans brief demotion. docs/07 §Instagram.

The export fixture tree is built here rather than checked in, in the test_imessage
spirit: a real Meta export is hundreds of files of ad-interest inventory, and the three
JSON keys this connector reads are the whole contract.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from backglass.brief import daily
from backglass.config import Settings
from backglass.connectors.boundary import Boundary
from backglass.connectors.instagram import (
    Allowlist,
    InstagramExportConnector,
    InstagramLiveConnector,
    _fix_mojibake,
)
from backglass.db import now_iso
from backglass.ledger import USER_ID, Ledger

TODAY = date(2026, 7, 30)

#: 2026-07-10 15:04:05 UTC in Meta's timestamp_ms.
TS_2026_07_10 = 1_783_695_845_000
EXPECTED_ISO = "2026-07-10T15:04:05+00:00"


def _mojibake(text: str) -> str:
    """Serialise a clean string the way Meta's export does: UTF-8 bytes read as latin-1."""
    return text.encode("utf-8").decode("latin-1")


def build_export(
    root: Path, threads: list[dict[str, Any]], *, layout: str = "your_instagram_activity"
) -> Path:
    """A minimal export folder: only the keys the connector actually reads."""
    inbox = root / layout / "messages" / "inbox"
    for thread in threads:
        folder = inbox / thread["key"]
        folder.mkdir(parents=True)
        (folder / "message_1.json").write_text(
            json.dumps(
                {
                    "title": thread.get("title", ""),
                    "participants": [{"name": n} for n in thread.get("participants", [])],
                    "messages": [
                        {"sender_name": s, "timestamp_ms": ts, "content": c}
                        for s, ts, c in thread.get("messages", [])
                    ],
                }
            ),
            encoding="utf-8",
        )
    return root


def make_export_connector(
    root: Path, boundary: Boundary, chats: list[str]
) -> InstagramExportConnector:
    return InstagramExportConnector(
        export_path=root, allowlist=Allowlist(chats), boundary=boundary
    )


# ── mojibake ──────────────────────────────────────────────────────────────


def test_fix_mojibake_repairs_metas_latin1_round_trip_and_leaves_clean_text_alone() -> None:
    assert _fix_mojibake(_mojibake("café 🎂")) == "café 🎂"
    assert _fix_mojibake("café 🎂") == "café 🎂"
    assert _fix_mojibake("plain ascii") == "plain ascii"


# ── export lane ───────────────────────────────────────────────────────────


def test_allowlisted_group_chat_is_read_and_the_rest_is_tallied_not_stored(
    tmp_path: Path, boundary: Boundary
) -> None:
    root = build_export(
        tmp_path,
        [
            {
                "key": "goatrip_123",
                "title": "Goa trip",
                "participants": ["K", "Priya", "Arjun"],
                "messages": [("Priya", TS_2026_07_10, "beach house saturday?")],
            },
            {
                "key": "memes_456",
                "title": "meme dump",
                "participants": ["K", "Rohan", "Dev"],
                "messages": [
                    ("Rohan", TS_2026_07_10 + 1000, "lol"),
                    ("Dev", TS_2026_07_10 + 2000, "lmao"),
                ],
            },
        ],
    )
    connector = make_export_connector(root, boundary, ["Goa trip"])
    items = list(connector.fetch(None))

    assert [item.body_text for item in items] == ["beach house saturday?"]
    assert items[0].title == "Goa trip"
    assert items[0].author == "Priya"
    assert items[0].occurred_at == EXPECTED_ISO
    assert connector.excluded == 2
    assert connector.excluded_by_rule == {"allowlist": 2}


def test_one_to_one_thread_matches_by_either_participant_name(
    tmp_path: Path, boundary: Boundary
) -> None:
    root = build_export(
        tmp_path,
        [
            {
                "key": "priya_789",
                "title": "Priya Sharma",
                "participants": ["K", "Priya Sharma"],
                "messages": [("Priya Sharma", TS_2026_07_10, "dinner friday?")],
            },
            {
                "key": "group_by_person_000",
                # A *group* chat never matches by participant — the owner named
                # chats and people, not "every group Priya is in".
                "title": "apartment hunt",
                "participants": ["K", "Priya Sharma", "Arjun"],
                "messages": [("Arjun", TS_2026_07_10, "saw a 2bhk")],
            },
        ],
    )
    connector = make_export_connector(root, boundary, ["priya sharma"])
    items = list(connector.fetch(None))
    assert [item.body_text for item in items] == ["dinner friday?"]


def test_mojibake_is_repaired_in_title_participants_and_content(
    tmp_path: Path, boundary: Boundary
) -> None:
    root = build_export(
        tmp_path,
        [
            {
                "key": "bday_111",
                "title": _mojibake("célébration 🎂"),
                "participants": ["K", _mojibake("Priyā")],
                "messages": [(_mojibake("Priyā"), TS_2026_07_10, _mojibake("café at 8 🎉"))],
            }
        ],
    )
    connector = make_export_connector(root, boundary, ["célébration 🎂"])
    items = list(connector.fetch(None))
    assert items[0].body_text == "café at 8 🎉"
    assert items[0].author == "Priyā"
    assert items[0].title == "célébration 🎂"


def test_system_strings_are_skipped_like_the_tapbacks_they_are(
    tmp_path: Path, boundary: Boundary
) -> None:
    root = build_export(
        tmp_path,
        [
            {
                "key": "goatrip_123",
                "title": "Goa trip",
                "participants": ["K", "Priya", "Arjun"],
                "messages": [
                    ("Priya", TS_2026_07_10 + 1, "Liked a message"),
                    ("Arjun", TS_2026_07_10 + 2, "Priya sent an attachment."),
                    ("Priya", TS_2026_07_10 + 3, "Reacted 🎂 to your message"),
                    ("Arjun", TS_2026_07_10 + 4, ""),
                    ("Priya", TS_2026_07_10 + 5, "flights booked for the 12th"),
                ],
            }
        ],
    )
    connector = make_export_connector(root, boundary, ["Goa trip"])
    items = list(connector.fetch(None))
    assert [item.body_text for item in items] == ["flights booked for the 12th"]


def test_cursor_advances_past_everything_and_a_second_fetch_yields_nothing(
    tmp_path: Path, boundary: Boundary
) -> None:
    """Idempotency at the connector seam — including excluded threads, which must not
    be re-tallied on every run forever (the imessage NULL-text lesson)."""
    root = build_export(
        tmp_path,
        [
            {
                "key": "goatrip_123",
                "title": "Goa trip",
                "participants": ["K", "Priya"],
                "messages": [("Priya", TS_2026_07_10, "beach house saturday?")],
            },
            {
                "key": "memes_456",
                "title": "meme dump",
                "participants": ["K", "Rohan"],
                "messages": [("Rohan", TS_2026_07_10 + 5000, "lol")],
            },
        ],
    )
    connector = make_export_connector(root, boundary, ["Goa trip"])
    first = list(connector.fetch(None))
    assert len(first) == 1
    # The watermark covers the excluded thread's newer message too.
    assert connector.cursor == str(TS_2026_07_10 + 5000)

    again = make_export_connector(root, boundary, ["Goa trip"])
    assert list(again.fetch(connector.cursor)) == []
    assert again.excluded == 0


def test_external_id_is_stable_across_re_exports(tmp_path: Path, boundary: Boundary) -> None:
    """Two exports of the same history must upsert, not duplicate: the thread directory
    name and timestamp_ms persist across exports, so the id does too."""
    spec = [
        {
            "key": "goatrip_123",
            "title": "Goa trip",
            "participants": ["K", "Priya"],
            "messages": [("Priya", TS_2026_07_10, "beach house saturday?")],
        }
    ]
    first = build_export(tmp_path / "a", spec)
    second = build_export(tmp_path / "b", spec)
    one = list(make_export_connector(first, boundary, ["Goa trip"]).fetch(None))
    two = list(make_export_connector(second, boundary, ["Goa trip"]).fetch(None))
    assert one[0].external_id == two[0].external_id
    assert one[0].content_hash == two[0].content_hash


def test_health_names_the_exact_next_step(tmp_path: Path, boundary: Boundary) -> None:
    missing = make_export_connector(tmp_path / "nope", boundary, ["Goa trip"])
    assert not missing.health().ok
    assert "Download your information" in (missing.health().detail or "")

    no_chats = make_export_connector(build_export(tmp_path, []), boundary, [])
    assert not no_chats.health().ok
    assert "INSTAGRAM_CHATS" in (no_chats.health().detail or "")

    html_export = tmp_path / "html"
    html_export.mkdir()
    wrong_format = make_export_connector(html_export, boundary, ["Goa trip"])
    assert not wrong_format.health().ok
    assert "JSON" in (wrong_format.health().detail or "")


def test_old_export_layout_without_activity_prefix_still_reads(
    tmp_path: Path, boundary: Boundary
) -> None:
    root = build_export(
        tmp_path,
        [
            {
                "key": "goatrip_123",
                "title": "Goa trip",
                "participants": ["K", "Priya"],
                "messages": [("Priya", TS_2026_07_10, "beach house saturday?")],
            }
        ],
        layout=".",
    )
    connector = make_export_connector(root, boundary, ["Goa trip"])
    assert connector.health().ok
    assert len(list(connector.fetch(None))) == 1


# ── live lane, against a fake client ──────────────────────────────────────


def fake_client(threads: list[SimpleNamespace]) -> Any:
    return SimpleNamespace(
        direct_threads=lambda amount: threads,
        direct_messages=lambda thread_id, amount: next(
            t for t in threads if t.id == thread_id
        ).messages,
    )


def a_thread(
    thread_id: str,
    title: str | None,
    users: list[tuple[int, str]],
    messages: list[tuple[str, int, str, datetime]],
) -> SimpleNamespace:
    return SimpleNamespace(
        id=thread_id,
        thread_title=title,
        users=[SimpleNamespace(pk=pk, username=name) for pk, name in users],
        messages=[
            SimpleNamespace(id=mid, user_id=uid, text=text, timestamp=ts)
            for mid, uid, text, ts in messages
        ],
    )


def make_live_connector(
    threads: list[SimpleNamespace], boundary: Boundary, chats: list[str]
) -> InstagramLiveConnector:
    return InstagramLiveConnector(
        username="k",
        session_file=None,
        allowlist=Allowlist(chats),
        boundary=boundary,
        client_factory=lambda: fake_client(threads),
    )


def test_live_lane_reads_only_allowlisted_threads_and_keeps_an_iso_cursor(
    boundary: Boundary,
) -> None:
    when = datetime(2026, 7, 10, 15, 4, 5, tzinfo=UTC)
    threads = [
        a_thread(
            "t1",
            "Goa trip",
            [(1, "priya.s"), (2, "arjun_k")],
            [("m1", 1, "beach house saturday?", when)],
        ),
        a_thread("t2", None, [(3, "randomshop")], [("m2", 3, "SALE 40% off", when)]),
    ]
    connector = make_live_connector(threads, boundary, ["Goa trip"])
    items = list(connector.fetch(None))

    assert [item.body_text for item in items] == ["beach house saturday?"]
    assert items[0].source == "instagram:live"
    assert items[0].author == "priya.s"
    assert items[0].occurred_at == EXPECTED_ISO
    assert connector.cursor == "2026-07-10T15:04:05+00:00"
    assert connector.excluded_by_rule == {"allowlist": 1}

    again = make_live_connector(threads, boundary, ["Goa trip"])
    assert list(again.fetch(connector.cursor)) == []


def windowed_client(threads: list[SimpleNamespace]) -> Any:
    """A client that honours `amount` the way instagrapi does: the newest N, not a page.

    The plain `fake_client` above returns every message whatever is asked for, so it can
    never express the case this connector's watermark logic exists for. Requested window
    sizes are recorded because "did it go back for a deeper one" is the behaviour under
    test, not just the item list.
    """
    requested: list[int] = []

    def direct_messages(thread_id: str, amount: int) -> list[SimpleNamespace]:
        requested.append(amount)
        messages = next(t for t in threads if t.id == thread_id).messages
        return sorted(messages, key=lambda m: m.timestamp, reverse=True)[:amount]

    return SimpleNamespace(
        direct_threads=lambda amount: threads,
        direct_messages=direct_messages,
        requested=requested,
    )


def a_backlog(count: int, *, start: datetime) -> list[tuple[str, int, str, datetime]]:
    return [
        (f"m{i}", 1, f"message {i}", start + timedelta(minutes=i)) for i in range(count)
    ]


def test_a_full_window_is_re_read_deeper_rather_than_skipped_over(
    boundary: Boundary,
) -> None:
    """The connector was down and the chat kept going: more new messages than one window.

    The newest 50 come back, the oldest 10 do not, and the pre-fix watermark moved to the
    newest of the 50 — putting the other 10 permanently below a floor no later run ever
    dips under. The window has to reach the watermark before it may be believed.
    """
    start = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    threads = [a_thread("t1", "Goa trip", [(1, "priya.s")], a_backlog(60, start=start))]
    client = windowed_client(threads)
    connector = InstagramLiveConnector(
        username="k",
        session_file=None,
        allowlist=Allowlist(["Goa trip"]),
        boundary=boundary,
        client_factory=lambda: client,
        max_messages_amount=100,
    )

    watermark = (start - timedelta(minutes=1)).isoformat()
    texts = [item.body_text for item in connector.fetch(watermark)]

    assert len(texts) == 60, "every message above the watermark reaches the ledger"
    assert client.requested == [50, 100], "the full first window sent it back for a deeper one"
    assert connector.truncated_threads == 0
    assert connector.cursor == (start + timedelta(minutes=59)).isoformat()


def test_a_backlog_deeper_than_the_ceiling_holds_the_watermark(boundary: Boundary) -> None:
    """Correctness over completeness: the lane would rather re-read than lose a message.

    Nothing below the deepest window is covered, so there is no safe point to advance to
    and the cursor is left unset — sync.py keeps the stored one, and the next run tries
    the same gap again. content_hash makes the overlap free (docs/03).
    """
    start = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    threads = [a_thread("t1", "Goa trip", [(1, "priya.s")], a_backlog(200, start=start))]
    client = windowed_client(threads)
    connector = InstagramLiveConnector(
        username="k",
        session_file=None,
        allowlist=Allowlist(["Goa trip"]),
        boundary=boundary,
        client_factory=lambda: client,
        max_messages_amount=100,
    )

    items = list(connector.fetch((start - timedelta(minutes=1)).isoformat()))

    assert len(items) == 100, "the deepest window it is allowed still ships what it read"
    assert connector.truncated_threads == 1
    assert connector.cursor is None, "the watermark must not step over the unread gap"


def test_a_first_run_takes_the_fixed_window_without_backfilling(boundary: Boundary) -> None:
    """No cursor means no coverage to be contiguous with, so there is no gap to lose.

    Deepening here would crawl the entire inbox history on day one, which is the ban risk
    the lane's gentle-polling defaults exist to avoid.
    """
    start = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    threads = [a_thread("t1", "Goa trip", [(1, "priya.s")], a_backlog(200, start=start))]
    client = windowed_client(threads)
    connector = InstagramLiveConnector(
        username="k",
        session_file=None,
        allowlist=Allowlist(["Goa trip"]),
        boundary=boundary,
        client_factory=lambda: client,
    )

    items = list(connector.fetch(None))

    assert len(items) == 50
    assert client.requested == [50]
    assert connector.truncated_threads == 0
    assert connector.cursor == (start + timedelta(minutes=199)).isoformat()


def test_live_lane_matches_a_one_to_one_thread_by_username(boundary: Boundary) -> None:
    when = datetime(2026, 7, 10, 15, 4, 5, tzinfo=UTC)
    threads = [
        a_thread("t1", None, [(1, "priya.s")], [("m1", 1, "dinner friday?", when)])
    ]
    connector = make_live_connector(threads, boundary, ["priya.s"])
    assert [i.body_text for i in connector.fetch(None)] == ["dinner friday?"]


def test_live_lane_health_requires_an_allowlist_even_with_a_client(
    boundary: Boundary,
) -> None:
    connector = make_live_connector([], boundary, [])
    assert not connector.health().ok
    assert "INSTAGRAM_CHATS" in (connector.health().detail or "")
    assert make_live_connector([], boundary, ["Goa trip"]).health().ok


# ── the owner's rule: friend plans ride low unless it's a special event ───


def seed_commitment(
    conn: Any,
    settings: Settings,
    *,
    source: str,
    what: str,
    due_at: str | None,
    direction: str = "i_owe",
    n: int = 1,
) -> None:
    ledger = Ledger(conn, settings)
    conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at, occurred_at, "
        " author, title, body_text, raw_json, content_hash, triage_verdict) "
        "VALUES (?, ?, ?, ?, '2026-07-28T09:15:00+00:00', 'Priya', 'Goa trip', 'b', '{}', ?, "
        " 'keep')",
        (USER_ID, source, f"ig{n}", now_iso(), f"ighash{n}"),
    )
    source_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    entity_id = ledger.resolve_entity("Priya")
    conn.execute(
        "INSERT INTO commitment (user_id, direction, counterparty_entity_id, what, due_at, "
        " confidence, status, source_item_id, created_at, rollover_count) "
        "VALUES (?, ?, ?, ?, ?, 0.9, 'open', ?, ?, 0)",
        (USER_ID, direction, entity_id, what, due_at, source_id, now_iso()),
    )


def _section(brief: Any, title: str) -> Any:
    return next((s for s in brief.sections if s.title == title), None)


def test_a_friend_plan_is_demoted_out_of_slipping_into_the_last_section(
    conn: Any, settings: Settings
) -> None:
    seed_commitment(
        conn, settings, source="instagram", what="book beach house for goa", due_at="2026-07-31"
    )
    brief = daily.build(conn, settings, for_date=TODAY)

    slipping = _section(brief, "Slipping")
    plans = _section(brief, "Friend plans")
    slipping_text = " ".join(line.text for line in slipping.lines) if slipping else ""
    assert "beach house" not in slipping_text
    assert plans is not None
    assert "book beach house for goa" in plans.lines[0].text
    # "Lower priority" in the brief's own mechanics: sorted last, truncated first (B1).
    assert plans.priority == max(s.priority for s in brief.sections)


def test_a_birthday_stays_in_slipping_at_full_priority(conn: Any, settings: Settings) -> None:
    seed_commitment(
        conn,
        settings,
        source="instagram",
        what="get cake for Priya's birthday",
        due_at="2026-07-31",
    )
    brief = daily.build(conn, settings, for_date=TODAY)

    slipping = _section(brief, "Slipping")
    assert slipping is not None
    assert "birthday" in slipping.lines[0].text
    assert _section(brief, "Friend plans") is None  # never said twice


def test_the_live_lane_source_and_awaiting_direction_are_demoted_too(
    conn: Any, settings: Settings
) -> None:
    seed_commitment(
        conn,
        settings,
        source="instagram:live",
        what="priya to send villa options",
        due_at=None,
        direction="owed_to_me",
    )
    brief = daily.build(conn, settings, for_date=TODAY)

    awaiting = _section(brief, "Awaiting others")
    assert awaiting is None or "villa" not in " ".join(line.text for line in awaiting.lines)
    plans = _section(brief, "Friend plans")
    assert plans is not None
    assert "villa options" in plans.lines[0].text


def test_special_event_wording_variants_are_recognised() -> None:
    demote = daily._demoted_friend_plan
    row = lambda what: {"source": "instagram", "what": what}  # noqa: E731
    assert not demote(row("dinner for Arjun's b-day"))
    assert not demote(row("anniversary gift ideas"))
    assert not demote(row("RSVP to the wedding"))
    assert demote(row("badminton on sunday"))
    assert demote({"source": "gmail:personal", "what": "badminton on sunday"}) is False
