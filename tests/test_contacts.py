"""The address book: who `+14802411748` is. Phase 13.

The runner is injected, so no test touches the automation bridge — same rule as every
other Apple source. Three things are load-bearing here and get a test each: one
canonical form for four ways of writing a number, an import that never overwrites what
the owner typed and writes nothing on a second run (rule 3), and an ambiguous number
that shows its digits rather than a confident wrong name.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backglass import chats as chats_mod
from backglass import contacts as contacts_mod
from backglass.config import Settings
from backglass.connectors import credentials
from backglass.connectors.boundary import Boundary, purge
from backglass.connectors.contacts import ContactsSource
from backglass.contacts import canonical
from backglass.ledger import USER_ID
from backglass.sync import sync
from backglass.web.app import create_app
from tests.conftest import FakeModel, panel_slice

CARDS = [
    {"id": "c-1", "name": "Saritha Kesavan", "org": "", "isCompany": False,
     "phones": ["(480) 241-1748"], "emails": ["saritha@example.com"]},
    {"id": "c-2", "name": "Pranav Sundhar", "org": "", "isCompany": False,
     "phones": ["+1 602 760 7180"], "emails": []},
    {"id": "c-3", "name": "", "org": "Dentist", "isCompany": True,
     "phones": ["480-555-0000"], "emails": []},
]


def fake_runner(payload: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    def run(script: str) -> str:
        del script
        return json.dumps(payload)

    return run


def entity(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM entity WHERE user_id = ? AND canonical_name = ?", (USER_ID, name)
    ).fetchone()
    return dict(row)


def seen(conn: sqlite3.Connection, *keys: str, kind: str = "dm") -> None:
    chats_mod.record(
        conn,
        "imessage",
        [chats_mod.Sighting(key=k, display_name=k, kind=kind) for k in keys],
    )


class TestCanonicalForm:
    @pytest.mark.parametrize(
        "written",
        ["+14802411748", "(480) 241-1748", "480-241-1748", "14802411748",
         "1 (480) 241 1748", "480.241.1748"],
    )
    def test_every_way_of_writing_one_number_compares_equal(self, written: str) -> None:
        """The whole job. Contacts writes `(480) 241-1748`; Messages writes
        `+14802411748`; neither is going to change, so comparison happens on one form."""
        assert canonical(written) == "+14802411748"

    def test_an_email_is_folded_not_digit_stripped(self) -> None:
        assert canonical("Loo123QW@Gmail.com") == "loo123qw@gmail.com"

    @pytest.mark.parametrize("value", ["", None, "topgolf", "pih ball", "66960", "7535"])
    def test_what_is_not_an_identifier_resolves_to_nothing(self, value: str) -> None:
        """A group name and a five-digit short code are not phone numbers. None means
        *do not attempt resolution*, which is what keeps `topgolf` off the lookup."""
        assert canonical(value) is None

    def test_an_international_number_keeps_its_own_country_code(self) -> None:
        assert canonical("+91 94430 14744") == "+919443014744"

    @pytest.mark.parametrize("value", ["11112000", "386767", "1234567", "12345678901234"])
    def test_a_bare_number_of_an_unreadable_length_is_not_invented_into_e164(
        self, value: str
    ) -> None:
        """Ten digits, or eleven starting with 1, are shapes this can read. Anything
        else bare is digits nobody said were a phone number, and `+{digits}` would be a
        fabricated key that can collide with a real one."""
        assert canonical(value) is None

    def test_the_shapes_it_can_read_still_read(self) -> None:
        """The other side of the rule above: refusing the unreadable must not cost the
        bare North American cards, which is where most of the real answers came from."""
        assert canonical("(480) 241-1748") == "+14802411748"
        assert canonical("1 (602) 758-6424") == "+16027586424"


class TestImport:
    def test_a_contact_becomes_a_person_with_its_identifiers(
        self, conn: sqlite3.Connection, boundary: Boundary
    ) -> None:
        source = ContactsSource(boundary=boundary, runner=fake_runner(CARDS))

        report = contacts_mod.import_contacts(conn, source.read())

        assert report.entities_created == 2  # the nameless company card is skipped
        assert json.loads(entity(conn, "Saritha Kesavan")["aliases_json"]) == [
            "+14802411748",
            "saritha@example.com",
        ]

    def test_no_source_item_is_written(
        self, conn: sqlite3.Connection, boundary: Boundary
    ) -> None:
        """A contact is not an event and carries no commitment. The immutable capture
        table is for evidence; an address book is reference data."""
        source = ContactsSource(boundary=boundary, runner=fake_runner(CARDS))

        contacts_mod.import_contacts(conn, source.read())

        assert conn.execute("SELECT count(*) AS n FROM source_item").fetchone()["n"] == 0

    def test_a_second_run_writes_nothing(
        self, conn: sqlite3.Connection, boundary: Boundary
    ) -> None:
        """Rule 3, on the one table the owner hand-edits."""
        source = ContactsSource(boundary=boundary, runner=fake_runner(CARDS))
        contacts_mod.import_contacts(conn, source.read())

        again = contacts_mod.import_contacts(conn, source.read())

        assert again.writes == 0
        assert again.aliases_added == 0

    def test_a_name_the_owner_corrected_survives_the_next_run(
        self, conn: sqlite3.Connection, boundary: Boundary
    ) -> None:
        """The People page edits these rows. A nightly sync that rewrote
        `canonical_name` would undo the correction and never say so."""
        source = ContactsSource(boundary=boundary, runner=fake_runner(CARDS))
        contacts_mod.import_contacts(conn, source.read())
        conn.execute(
            "UPDATE entity SET canonical_name = ?, role = ? WHERE canonical_name = ?",
            ("Amma", "family", "Saritha Kesavan"),
        )

        contacts_mod.import_contacts(conn, source.read())

        edited = entity(conn, "Amma")
        assert edited["role"] == "family"
        assert conn.execute(
            "SELECT count(*) AS n FROM entity WHERE canonical_name = 'Saritha Kesavan'"
        ).fetchone()["n"] == 0

    def test_an_identifier_joins_a_person_the_extractor_already_created(
        self, conn: sqlite3.Connection, boundary: Boundary
    ) -> None:
        """The point of writing to `entity`: the person the owner emails and the person
        they text become one row, so the awaiting view stops fragmenting."""
        conn.execute(
            "INSERT INTO entity (user_id, kind, canonical_name, aliases_json)"
            " VALUES (?, 'person', 'Pranav Sundhar', '[\"pranav@example.com\"]')",
            (USER_ID,),
        )
        source = ContactsSource(boundary=boundary, runner=fake_runner(CARDS))

        contacts_mod.import_contacts(conn, source.read())

        assert json.loads(entity(conn, "Pranav Sundhar")["aliases_json"]) == [
            "pranav@example.com",
            "+16027607180",
        ]

    def test_a_number_two_people_claim_is_linked_to_neither(
        self, conn: sqlite3.Connection, boundary: Boundary
    ) -> None:
        shared = [
            {"id": "a", "name": "Dana Whitfield", "org": "", "isCompany": False,
             "phones": ["480-241-1748"], "emails": []},
            {"id": "b", "name": "Whitfield Household", "org": "", "isCompany": False,
             "phones": ["(480) 241-1748"], "emails": []},
        ]
        source = ContactsSource(boundary=boundary, runner=fake_runner(shared))

        report = contacts_mod.import_contacts(conn, source.read())

        assert report.ambiguous == ["+14802411748"]
        assert report.entities_created == 0
        assert contacts_mod.resolve(conn, ["+14802411748"])["+14802411748"].name is None

    def test_a_dry_run_writes_nothing(
        self, conn: sqlite3.Connection, boundary: Boundary
    ) -> None:
        source = ContactsSource(boundary=boundary, runner=fake_runner(CARDS))

        report = contacts_mod.import_contacts(conn, source.read(), dry_run=True)

        assert report.entities_created == 2
        assert conn.execute("SELECT count(*) AS n FROM entity").fetchone()["n"] == 0

    def test_the_preview_counts_what_the_run_actually_does(
        self, conn: sqlite3.Connection, boundary: Boundary
    ) -> None:
        """The owner's book holds both `Saritha Kesavan` and `saritha kesavan`. A real
        run inserts the first and folds the second into it; a preview that queries only
        the database cannot see the row it did not insert, and reported one more person
        and one fewer update than the run it was previewing."""
        twice = [
            {"id": "d-1", "name": "Saritha Kesavan", "org": "", "isCompany": False,
             "phones": ["(480) 241-1748"], "emails": []},
            {"id": "d-2", "name": "saritha kesavan", "org": "", "isCompany": False,
             "phones": [], "emails": ["saritha@example.com"]},
        ]
        source = ContactsSource(boundary=boundary, runner=fake_runner(twice))

        preview = contacts_mod.import_contacts(conn, source.read(), dry_run=True)
        real = contacts_mod.import_contacts(conn, source.read())

        assert (preview.entities_created, preview.entities_updated) == (1, 1)
        assert (preview.entities_created, preview.entities_updated) == (
            real.entities_created,
            real.entities_updated,
        )
        assert preview.aliases_added == real.aliases_added == 2

    def test_the_preview_does_not_recount_an_identifier_it_already_pended(
        self, conn: sqlite3.Connection, boundary: Boundary
    ) -> None:
        """Three cards under one name. The second adds an identifier to the row the first
        would have created; the third repeats the second's, and must count as nothing —
        the same zero-write rule, applied inside a single pass."""
        thrice = [
            {"id": "e-1", "name": "Bhanu", "org": "", "isCompany": False,
             "phones": ["(623) 280-8906"], "emails": []},
            {"id": "e-2", "name": "Bhanu", "org": "", "isCompany": False,
             "phones": [], "emails": ["bhanu@example.com"]},
            {"id": "e-3", "name": "Bhanu", "org": "", "isCompany": False,
             "phones": [], "emails": ["Bhanu@Example.com"]},
        ]
        source = ContactsSource(boundary=boundary, runner=fake_runner(thrice))

        preview = contacts_mod.import_contacts(conn, source.read(), dry_run=True)
        real = contacts_mod.import_contacts(conn, source.read())

        assert (preview.entities_created, preview.entities_updated) == (1, 1)
        assert (preview.entities_created, preview.entities_updated) == (
            real.entities_created,
            real.entities_updated,
        )
        assert preview.aliases_added == real.aliases_added == 2


class TestTheSource:
    def test_a_denied_bridge_is_a_product_state_not_a_traceback(
        self, boundary: Boundary
    ) -> None:
        def refuse(script: str) -> str:
            raise RuntimeError("Not authorised to send Apple events to Contacts")

        health = ContactsSource(boundary=boundary, runner=refuse).health()

        assert not health.ok
        assert "Automation" in (health.detail or "")

    def test_a_denylisted_card_never_becomes_an_entity(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """D1/D4: the boundary runs in the source, before persistence."""
        boundary = Boundary(mode="client_scoped", deny_domains=["clientco.com"])
        cards = [
            {"id": "x", "name": "Client Contact", "org": "", "isCompany": False,
             "phones": ["480-555-1212"], "emails": ["dana@clientco.com"]},
        ]
        source = ContactsSource(boundary=boundary, runner=fake_runner(cards))

        contacts_mod.import_contacts(conn, source.read())

        assert source.excluded == 1
        assert conn.execute("SELECT count(*) AS n FROM entity").fetchone()["n"] == 0


class TestInsideTheSync:
    def test_the_address_book_is_folded_in_before_ingest(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Boundary
    ) -> None:
        source = ContactsSource(boundary=boundary, runner=fake_runner(CARDS))

        first = sync(conn, settings, [], FakeModel(), contacts_source=source)
        second = sync(conn, settings, [], FakeModel(), contacts_source=source)

        assert first.contacts_linked == 3  # two numbers and an address
        assert first.writes == 2
        assert second.writes == 0  # rule 3
        assert credentials.load(conn, "apple-contacts").status == "ok"

    def test_pausing_the_source_actually_stops_it_reading_the_address_book(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Boundary
    ) -> None:
        """The privacy control, on the one source that reads the owner's whole address
        book. `sources disable` printed a confirmation, set `enabled = 0`, and the next
        sync read all 77 cards and wrote 68 rows anyway — while the Sources panel drew a
        paused square and a Resume button beside it. An inert control that reports
        success is worse than no control.
        """
        reads = []

        def counting_runner(script: str) -> str:
            reads.append(script)
            return json.dumps(CARDS)

        source = ContactsSource(boundary=boundary, runner=counting_runner)
        credentials.set_enabled(conn, "apple-contacts", False)

        report = sync(conn, settings, [], FakeModel(), contacts_source=source)

        assert reads == []  # the bridge is never even asked
        assert report.writes == 0
        assert report.contacts_linked == 0
        assert conn.execute("SELECT count(*) AS n FROM entity").fetchone()["n"] == 0
        assert credentials.disabled_sources(conn) == {"apple-contacts"}

    def test_the_sources_panel_says_paused_about_a_source_that_is_paused(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Boundary
    ) -> None:
        """The panel was never wrong about the flag — it was right about a flag that
        meant nothing, which is the worse failure: it confirmed a belief the sync was
        busy contradicting. Assert the two together, or this can drift apart again."""
        credentials.set_enabled(conn, "apple-contacts", False)
        source = ContactsSource(boundary=boundary, runner=fake_runner(CARDS))
        sync(conn, settings, [], FakeModel(), contacts_source=source)
        conn.commit()
        client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")

        panel = panel_slice(client.get("/").text, "panel-sources")

        assert "apple-contacts" in panel
        assert "Resume" in panel  # the button the paused state renders
        assert conn.execute("SELECT count(*) AS n FROM entity").fetchone()["n"] == 0

    def test_resuming_it_starts_reading_again(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Boundary
    ) -> None:
        """The other half — a pause that cannot be lifted is its own bug."""
        source = ContactsSource(boundary=boundary, runner=fake_runner(CARDS))
        credentials.set_enabled(conn, "apple-contacts", False)
        sync(conn, settings, [], FakeModel(), contacts_source=source)

        credentials.set_enabled(conn, "apple-contacts", True)
        report = sync(conn, settings, [], FakeModel(), contacts_source=source)

        assert report.contacts_linked == 3
        assert conn.execute("SELECT count(*) AS n FROM entity").fetchone()["n"] == 2

    def test_a_paused_source_is_not_built_and_not_health_checked(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """`doctor` and the CLI go through one constructor, so the pause holds there too
        — otherwise doctor prompts for Automation permission for a source that is off."""
        from backglass.__main__ import _contacts_source

        enabled = settings.model_copy(update={"apple_contacts": True})
        assert _contacts_source(conn, enabled) is not None

        credentials.set_enabled(conn, "apple-contacts", False)

        assert _contacts_source(conn, enabled) is None

    def test_a_denied_bridge_degrades_and_does_not_block(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Boundary
    ) -> None:
        """Rule 5. A nameless number is worse than yesterday's names, and very much
        better than a sync that refused to run."""

        def refuse(script: str) -> str:
            raise RuntimeError("Not authorised to send Apple events to Contacts")

        report = sync(
            conn,
            settings,
            [],
            FakeModel(),
            contacts_source=ContactsSource(boundary=boundary, runner=refuse),
        )

        assert report.failed_sources == ["apple-contacts"]
        assert report.exit_code == 1
        assert credentials.load(conn, "apple-contacts").status == "failed"


class TestTheBoundaryPurge:
    """docs/08 D6, rule 6. Adding a domain has to reach what is already stored."""

    def test_a_later_denylist_addition_strips_the_stored_identifier(
        self, conn: sqlite3.Connection
    ) -> None:
        """The card was imported while the denylist was empty, which is the whole point
        of D6: "the denylist is incomplete on day one". Until this, an address written
        into `entity.aliases_json` survived the purge that was supposed to remove it, and
        docs/08 says excluded content is not retained in any form."""
        cards = [
            {"id": "y", "name": "Dana Whitfield", "org": "", "isCompany": False,
             "phones": ["480-555-1212"], "emails": ["dana@clientco.com"]},
        ]
        open_book = ContactsSource(
            boundary=Boundary(mode="full_scope"), runner=fake_runner(cards)
        )
        contacts_mod.import_contacts(conn, open_book.read())
        assert "dana@clientco.com" in entity(conn, "Dana Whitfield")["aliases_json"]

        corrected = Boundary(mode="exclude", deny_domains=["clientco.com"])
        report = purge(conn, corrected)

        assert report.entity_identifiers == 1
        assert report.matched_rules == {"clientco.com": 1}
        aliases = json.loads(entity(conn, "Dana Whitfield")["aliases_json"])
        assert aliases == ["+14805551212"]  # the person stays; the address does not
        assert contacts_mod.resolve(conn, ["dana@clientco.com"]) == {
            "dana@clientco.com": contacts_mod.Resolution(key="dana@clientco.com")
        }

    def test_a_dry_run_purge_reports_without_stripping(
        self, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO entity (user_id, kind, canonical_name, aliases_json)"
            " VALUES (?, 'person', 'Dana Whitfield', '[\"dana@clientco.com\"]')",
            (USER_ID,),
        )
        corrected = Boundary(mode="exclude", deny_domains=["clientco.com"])

        report = purge(conn, corrected, dry_run=True)

        assert report.entity_identifiers == 1
        assert "dana@clientco.com" in entity(conn, "Dana Whitfield")["aliases_json"]

    def test_identifiers_that_match_nothing_are_left_exactly_as_they_were(
        self, conn: sqlite3.Connection, boundary: Boundary
    ) -> None:
        """A purge is a deletion, so the test that it does not over-reach matters as
        much as the test that it reaches at all."""
        contacts_mod.import_contacts(
            conn, ContactsSource(boundary=boundary, runner=fake_runner(CARDS)).read()
        )
        before = entity(conn, "Saritha Kesavan")["aliases_json"]

        purge(conn, Boundary(mode="exclude", deny_domains=["clientco.com"]))

        assert entity(conn, "Saritha Kesavan")["aliases_json"] == before


class TestTheConversationsPage:
    def test_a_bare_number_is_named_and_still_shows_its_digits(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Boundary
    ) -> None:
        """The bug this was built for: the consent prompt asked whether to read
        `+14802411748`. The name answers the question; the number keeps it checkable."""
        seen(conn, "+14802411748")
        contacts_mod.import_contacts(
            conn, ContactsSource(boundary=boundary, runner=fake_runner(CARDS)).read()
        )
        conn.commit()
        client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")

        waiting = panel_slice(client.get("/chats").text, "panel-waiting")

        assert "Saritha Kesavan" in waiting
        assert "+14802411748" in waiting

    def test_an_unknown_number_is_left_alone(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Boundary
    ) -> None:
        seen(conn, "+16232578644")
        contacts_mod.import_contacts(
            conn, ContactsSource(boundary=boundary, runner=fake_runner(CARDS)).read()
        )
        conn.commit()
        client = TestClient(create_app(settings), base_url="http://127.0.0.1:8765")

        waiting = panel_slice(client.get("/chats").text, "panel-waiting")

        assert "+16232578644" in waiting

    def test_a_group_name_is_not_treated_as_an_identifier(
        self, conn: sqlite3.Connection
    ) -> None:
        seen(conn, "Topgolf", kind="group")

        (chat,) = chats_mod.listing(conn)

        assert chat.label == "Topgolf"
        assert chat.identifier is None
