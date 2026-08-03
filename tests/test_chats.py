"""Monitored conversations: what the ledger reads, and what it is waiting to be told.

The decision layer, the connector's sightings, and the page that asks. The point of the
feature is the state that did not exist before — *seen but not yet decided* — so most of
these are about that.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backglass import chats as chats_mod
from backglass.config import Settings
from backglass.web.app import create_app


def seen(conn: sqlite3.Connection, *names: str, source: str = "imessage") -> None:
    chats_mod.record(
        conn, source, [chats_mod.Sighting(key=n, display_name=n) for n in names]
    )


class TestDecisions:
    def test_a_conversation_is_never_monitored_by_default(
        self, conn: sqlite3.Connection
    ) -> None:
        """The whole feature. A chat nobody has answered for is not "off" — it is a
        question, and consent to read one group says nothing about the next."""
        seen(conn, "plague spreaders")

        (chat,) = chats_mod.listing(conn)

        assert chat.decision is None
        assert chat.undecided
        assert not chats_mod.allowlist_for(conn, "imessage").allows(
            title="plague spreaders", participants=[]
        )

    def test_monitoring_admits_it_to_the_allowlist(self, conn: sqlite3.Connection) -> None:
        seen(conn, "Pih ball")
        (chat,) = chats_mod.listing(conn)

        assert chats_mod.decide(conn, chat.id, chats_mod.MONITOR)

        assert chats_mod.allowlist_for(conn, "imessage").allows(
            title="Pih ball", participants=[]
        )

    def test_ignoring_is_remembered_so_it_is_never_asked_twice(
        self, conn: sqlite3.Connection
    ) -> None:
        """Without a stored `ignore`, every sync would re-raise every group the owner has
        already declined — and a prompt that repeats itself is one people stop reading."""
        seen(conn, "work spam")
        (chat,) = chats_mod.listing(conn)
        chats_mod.decide(conn, chat.id, chats_mod.IGNORE)

        seen(conn, "work spam")  # the next sync sees it again

        assert chats_mod.undecided(conn) == []
        assert chats_mod.listing(conn)[0].decision == chats_mod.IGNORE

    def test_seeing_it_again_does_not_reopen_the_question(
        self, conn: sqlite3.Connection
    ) -> None:
        seen(conn, "Pih ball")
        (chat,) = chats_mod.listing(conn)
        chats_mod.decide(conn, chat.id, chats_mod.MONITOR)

        seen(conn, "Pih ball")

        (again,) = chats_mod.listing(conn)
        assert again.decision == chats_mod.MONITOR
        assert again.messages_seen == 2, "activity still accrues"

    def test_the_same_name_on_two_services_is_two_decisions(
        self, conn: sqlite3.Connection
    ) -> None:
        """"Family" on iMessage and "Family" on Instagram are different conversations."""
        seen(conn, "Family", source="imessage")
        seen(conn, "Family", source="instagram")
        for chat in chats_mod.listing(conn, source="imessage"):
            chats_mod.decide(conn, chat.id, chats_mod.MONITOR)

        assert chats_mod.allowlist_for(conn, "imessage").allows(title="Family", participants=[])
        assert not chats_mod.allowlist_for(conn, "instagram").allows(
            title="Family", participants=[]
        )

    def test_an_unknown_decision_is_refused(self, conn: sqlite3.Connection) -> None:
        seen(conn, "Pih ball")
        (chat,) = chats_mod.listing(conn)
        with pytest.raises(ValueError):
            chats_mod.decide(conn, chat.id, "maybe")


class TestEnvSeeding:
    def test_an_existing_env_allowlist_keeps_working(self, conn: sqlite3.Connection) -> None:
        """The migration path. A machine configured the old way must not go dark the
        moment this lands."""
        chats_mod.seed_from_env(conn, "imessage", ["Pih ball", "Topgolf"])

        allowed = chats_mod.allowlist_for(conn, "imessage")
        assert allowed.allows(title="Pih ball", participants=[])
        assert [c.decision for c in chats_mod.listing(conn)] == ["monitor", "monitor"]

    def test_seeding_twice_creates_nothing_new(self, conn: sqlite3.Connection) -> None:
        chats_mod.seed_from_env(conn, "imessage", ["Pih ball"])
        assert chats_mod.seed_from_env(conn, "imessage", ["Pih ball"]) == 0

    def test_a_click_beats_a_stale_setting(self, conn: sqlite3.Connection) -> None:
        """`.env` is an artefact of how this used to work; a button is a decision made
        now. Re-seeding must not silently re-enable something the owner turned off."""
        chats_mod.seed_from_env(conn, "imessage", ["Pih ball"])
        (chat,) = chats_mod.listing(conn)
        chats_mod.decide(conn, chat.id, chats_mod.IGNORE)

        chats_mod.seed_from_env(conn, "imessage", ["Pih ball"])

        assert chats_mod.listing(conn)[0].decision == chats_mod.IGNORE


class TestTheConnectorReportsWhatItSaw:
    def test_a_chat_outside_the_allowlist_is_still_reported(
        self, tmp_path: Any, boundary: Any
    ) -> None:
        """The failure this replaces: an unnamed conversation was dropped in silence, so a
        new group chat where plans were being made never surfaced at all."""
        from backglass.connectors.allowlist import Allowlist
        from backglass.connectors.imessage import IMessageConnector
        from tests.test_imessage import build_store

        store = build_store(
            tmp_path / "chat.db",
            [
                {"rowid": 1, "handle": "+1555", "text": "hi", "chat": "Phoenix build"},
                {"rowid": 2, "handle": "+1999", "text": "new", "chat": "Brand new group"},
            ],
        )
        connector = IMessageConnector(
            db_path=store, boundary=boundary, allowlist=Allowlist(("Phoenix build",))
        )

        list(connector.fetch(None))

        assert set(connector.seen_chats) == {"Phoenix build", "Brand new group"}

    def test_a_one_to_one_is_reported_under_its_handle(
        self, tmp_path: Any, boundary: Any
    ) -> None:
        """Keyed the way the allowlist matches, or the page would offer a button that
        turns on something the connector then fails to recognise."""
        from backglass.connectors.allowlist import Allowlist
        from backglass.connectors.imessage import IMessageConnector
        from tests.test_imessage import build_store

        store = build_store(
            tmp_path / "dm.db", [{"rowid": 1, "handle": "+14805551212", "text": "hi"}]
        )
        connector = IMessageConnector(
            db_path=store, boundary=boundary, allowlist=Allowlist(())
        )

        list(connector.fetch(None))

        assert connector.seen_chats["+14805551212"].kind == "dm"


class TestThePage:
    def test_a_new_conversation_is_offered_for_a_decision(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        seen(conn, "Brand new group")
        conn.commit()
        client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")

        body = client.get("/chats").text

        assert "Brand new group" in body
        assert "Monitor" in body and "Ignore" in body

    def test_pressing_monitor_starts_it_being_read(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        seen(conn, "Pih ball")
        conn.commit()
        (chat,) = chats_mod.listing(conn)
        client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")

        response = client.post(f"/chats/{chat.id}/monitor", follow_redirects=False)

        assert response.status_code == 303
        assert chats_mod.allowlist_for(conn, "imessage").allows(
            title="Pih ball", participants=[]
        )

    def test_a_nonsense_decision_is_refused(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The decision arrives as a path segment, so anything can be sent."""
        seen(conn, "Pih ball")
        conn.commit()
        (chat,) = chats_mod.listing(conn)
        client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")

        assert client.post(f"/chats/{chat.id}/delete-everything").status_code == 422

    def test_the_dashboard_raises_the_question(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """A page nobody visits is not a prompt. An undecided conversation has to reach
        the surface the owner already looks at."""
        from tests.conftest import healthy_run, todays_plan

        healthy_run(conn)
        todays_plan(conn, settings)
        seen(conn, "Brand new group")
        conn.commit()
        client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")

        body = client.get("/").text

        assert "new conversation" in body
