"""Provenance links have to land somewhere. CLAUDE.md rule 1, checked at the reader's end.

Every claim linked to its source and the links were broken: `SourceRef.url` built
`/source/<external_id>` and `LedgerRef.url` built `/{table}/{row_id}`, and of those, not
one path was served. `/commitments/21` and `/checklist/3` were POST-only write endpoints,
so following one from an email got a 405; everything else got a 404. Because the ledger
holds no Gmail until the owner authenticates it, that was *every* provenance link in the
brief.

Rule 1 is checkable at the writing end (B2 refuses to render a line without provenance)
and was not checkable at the reading end at all. `test_every_brief_link_resolves` is that
missing half: it builds a real brief over a seeded ledger and asks the app whether each
link it produced is a page.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Route

from backglass.brief import daily
from backglass.brief.model import LedgerRef, SourceRef
from backglass.config import Settings
from backglass.db import now_iso
from backglass.extract.commitments import apply
from backglass.ledger import Ledger
from backglass.web.app import create_app
from tests.conftest import healthy_run
from tests.test_evidence import QUOTE, an_extraction, an_item

BASE = "http://127.0.0.1:8765"
TODAY = date(2026, 7, 30)


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn
    return TestClient(create_app(settings), base_url=BASE)


def seeded(conn: sqlite3.Connection, settings: Settings) -> int:
    """A ledger with one extracted commitment, its citation, and a completed run."""
    healthy_run(conn)
    item_id = an_item(conn, "note-1", QUOTE)
    apply(
        an_extraction(QUOTE),
        source_item_id=item_id,
        occurred_at="2026-07-10T09:15:00-07:00",
        ledger=Ledger(conn, settings),
        settings=settings,
    )
    return item_id


class TestTheSourcePage:
    def test_it_shows_the_raw_text_and_what_came_out_of_it(
        self, client: TestClient, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        item_id = seeded(conn, settings)
        page = client.get(f"/source/{item_id}")
        assert page.status_code == 200
        body = page.text
        assert "Send the housing deposit receipt" in body, "the derived commitment"
        assert "housing deposit receipt by Friday" in body, "the sentence it rests on"
        assert "triage: keep" in body, "and why the item was read at all"

    def test_a_restatement_appears_on_the_page_of_the_item_that_restated_it(
        self, client: TestClient, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The reverse question — "what did this document produce?" — has to see
        citations, not just `commitment.source_item_id`. A restating message produces no
        row of its own, and before citations existed it looked like a document that
        produced nothing."""
        seeded(conn, settings)
        restated = "Still owe you that receipt — Friday at the latest."
        second = an_item(conn, "note-2", restated)
        apply(
            an_extraction(restated),
            source_item_id=second,
            occurred_at="2026-07-12T09:15:00-07:00",
            ledger=Ledger(conn, settings),
            settings=settings,
        )
        body = client.get(f"/source/{second}").text
        assert "Send the housing deposit receipt" in body
        assert "restated" in body, "and says it is a restatement, not the origin"

    def test_an_unknown_item_is_an_honest_404(self, client: TestClient) -> None:
        assert client.get("/source/98765").status_code == 404

    def test_the_page_is_read_only(self, client: TestClient, conn: sqlite3.Connection,
                                   settings: Settings) -> None:
        """Everything on it is immutable or owned by another surface's write path.

        Also the 2026-08-01 lesson, structurally: a dashboard surface that can write is a
        surface that can be written to by accident during a browser test.
        """
        item_id = seeded(conn, settings)
        assert client.post(f"/source/{item_id}").status_code == 405


class TestBriefLinksResolve:
    def _paths(self, links: list[str]) -> list[str]:
        """Local paths only. An external deep-link (Gmail) is not ours to route."""
        return [urlsplit(u).path for u in links if u.startswith(BASE)]

    def test_every_brief_link_resolves(
        self, client: TestClient, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        seeded(conn, settings)
        brief = daily.build(conn, settings, TODAY)
        links = [
            line.provenance.url(BASE) for section in brief.sections for line in section.lines
        ]
        assert links, "a brief with no provenance links would make this test vacuous"

        for path in self._paths(links):
            response = client.get(path)
            assert response.status_code == 200, (
                f"provenance link {path} is not a page (HTTP {response.status_code}) — "
                "a claim whose source cannot be opened is not sourced"
            )

    def test_the_old_shapes_are_the_ones_that_would_have_failed(
        self, client: TestClient
    ) -> None:
        """Guards the guard. If `/source/<external_id>` and `/{table}/{row_id}` were
        routable after all, the test above would pass for the wrong reason and the
        original defect would not have been a defect."""
        assert client.get("/source/some-external-id").status_code != 200
        assert client.get("/runs/1").status_code != 200
        assert client.get("/plans/2026-07-30").status_code != 200
        assert client.get("/commitments/1").status_code != 200


class TestRefsPointAtRealRoutes:
    def _served_paths(self, client: TestClient) -> set[str]:
        """Every GET path the app serves, routers included.

        Walked recursively: this FastAPI version wraps each included router in an opaque
        `_IncludedRouter` whose own routes hang off `original_router`, so a flat pass
        over `app.routes` sees the dashboard and nothing else — which would make the
        assertion below fail for a reason unrelated to what it checks.
        """

        def walk(routes: object) -> set[str]:
            found: set[str] = set()
            for route in routes or []:  # type: ignore[union-attr]
                if isinstance(route, Route) and "GET" in (route.methods or set()):
                    found.add(route.path)
                nested = getattr(route, "routes", None)
                inner = getattr(route, "original_router", None)
                found |= walk(nested)
                found |= walk(getattr(inner, "routes", None))
            return found

        return walk(client.app.routes)  # type: ignore[attr-defined]

    def test_source_refs_use_the_row_id_not_the_external_id(
        self, client: TestClient
    ) -> None:
        """`external_id` is unique only *within* a source, so two connectors can hand
        back the same string. The page is keyed by rowid for that reason."""
        ref = SourceRef(
            source="apple-notes",
            external_id="x-1",
            occurred_at="2026-07-10T09:15:00-07:00",
            source_item_id=42,
        )
        assert ref.url(BASE) == f"{BASE}/source/42"
        assert "/source/{source_item_id}" in self._served_paths(client)

    def test_gmail_still_deep_links_into_the_account(self) -> None:
        ref = SourceRef(
            source="gmail:personal",
            external_id="18f00",
            occurred_at="2026-07-10T09:15:00-07:00",
            source_item_id=42,
        )
        assert ref.url(BASE) == "https://mail.google.com/mail/u/0/#all/18f00"

    @pytest.mark.parametrize(
        "table,row_id",
        [
            ("plans", "2026-07-30"),
            ("goals", "3"),
            ("sources", "gmail:personal"),
            ("runs", "7"),
            ("checklist", "5"),
            ("commitments", "21"),
            ("something-nobody-has-written-yet", "1"),
        ],
    )
    def test_every_ledger_ref_lands_on_a_page(
        self, client: TestClient, table: str, row_id: str
    ) -> None:
        """Including the fallback: a table added later must degrade to a live page, not
        to a 404 nobody notices until a reader clicks it."""
        url = LedgerRef(table, row_id, "described").url(BASE)
        assert client.get(urlsplit(url).path or "/").status_code == 200


class TestBoardProvenance:
    def test_a_board_row_links_to_its_source_page(
        self, client: TestClient, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The dashboard used to name the source without a link for everything except
        Gmail, on the honest grounds that no page existed. One does now."""
        item_id = seeded(conn, settings)
        assert f'href="/source/{item_id}"' in client.get("/").text

    def test_a_restated_commitment_says_how_many_documents_say_it(
        self, client: TestClient, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        seeded(conn, settings)
        restated = "Still owe you that receipt — Friday at the latest."
        second = an_item(conn, "note-2", restated)
        apply(
            an_extraction(restated),
            source_item_id=second,
            occurred_at="2026-07-12T09:15:00-07:00",
            ledger=Ledger(conn, settings),
            settings=settings,
        )
        assert "2 mentions" in client.get("/").text


def test_a_manual_commitment_is_still_sourced(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Quick-add writes a `manual` source item first (actions.quick_add), so even a
    hand-typed row has a page behind its provenance link. Rule 1 has no exception for
    claims the owner made themselves."""
    healthy_run(conn)
    client.post("/commitments/quick-add", data={"what": "Return the lab keys",
                                                "direction": "i_owe"})
    row = conn.execute(
        "SELECT source_item_id AS s FROM commitment ORDER BY id DESC"
    ).fetchone()
    item_id = int(row["s"])
    page = client.get(f"/source/{item_id}")
    assert page.status_code == 200
    assert "Return the lab keys" in page.text
    assert now_iso()[:4] in page.text, "with the date it was typed"
