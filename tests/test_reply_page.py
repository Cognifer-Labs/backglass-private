"""The Reply panel on the source page: when it appears, and what it never does.

The model is monkeypatched at the client factory, so no test here calls a live API. What
these assert is the part the model cannot get wrong on its own: that the panel is absent
where a reply makes no sense, that a failure degrades into a sentence instead of a broken
swap, and that the page writes nothing whichever way the call goes.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backglass.config import Settings
from backglass.ledger import USER_ID
from backglass.web.app import create_app
from tests.conftest import panel_slice

BODY = "Thank you, Dharsan. Very impressive. I am looking forward to working with you!"


def _drafted_body(html: str) -> str:
    """The email itself, out of the fragment.

    Bounded by the `draftmail` class rather than by element order or a closing tag, for
    the reason conftest's `panel_slice` exists: a split on `</pre>` breaks the first time
    anything nests inside it (tests/conftest.py, CLAUDE.md testing expectations).
    """
    marker = '<pre class="b1 draftmail">'
    start = html.index(marker) + len(marker)
    return html[start : html.index("</pre>", start)]



class FakeResult:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.cost_usd = 0.02


class FakeClient:
    def __init__(self, data: dict[str, Any] | None = None, boom: bool = False) -> None:
        self.data = data or {
            "subject": "Re: Thank you - Dharsan Kesavan",
            "body": "Dr. Shufeldt,\n\nCould we define the scope before I start?\n\nDharsan",
        }
        self.boom = boom

    def complete(self, **_kwargs: Any) -> FakeResult:
        if self.boom:
            raise RuntimeError("upstream 429")
        return FakeResult(self.data)


@pytest.fixture
def fake_model(monkeypatch: pytest.MonkeyPatch):
    """Swap the real backend out at its factory, for every route that builds one."""

    def _install(client_obj: FakeClient) -> FakeClient:
        from backglass.extract import client as client_mod

        monkeypatch.setattr(client_mod, "build", lambda _settings: client_obj)
        return client_obj

    return _install


def _mail(
    conn: sqlite3.Connection,
    *,
    author: str = "John Shufeldt <jshufeldt@example.com>",
    title: str = "Thank you - Dharsan Kesavan",
    occurred_at: str = "2026-08-22T20:06:59+00:00",
    body: str = BODY,
    source: str = "apple-mail",
) -> int:
    cur = conn.execute(
        "INSERT INTO source_item (user_id, source, external_id, fetched_at,"
        " occurred_at, author, title, body_text, content_hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            USER_ID, source, f"ext-{occurred_at}-{author}", "2026-08-25T00:00:00Z",
            occurred_at, author, title, body, f"hash-{occurred_at}-{author}",
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


@pytest.fixture
def client(conn: sqlite3.Connection, settings: Settings) -> TestClient:
    del conn
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8765")


class TestThePanel:
    def test_a_mail_item_offers_a_reply(self, conn, client) -> None:
        page = client.get(f"/source/{_mail(conn)}")
        assert page.status_code == 200
        panel = panel_slice(page.text, "panel-reply")
        assert "Reply to this" in panel
        assert "drafts, never sends" in panel

    def test_a_calendar_item_offers_nothing(self, conn, client) -> None:
        """A Draft button that can only produce an error is worse than no button."""
        item = _mail(
            conn, source="calendar:asu", author="", title="LSB 191",
            occurred_at="2026-08-25T14:30:00-07:00",
        )
        assert 'id="panel-reply"' not in client.get(f"/source/{item}").text

    def test_the_panel_says_it_never_sends(self, conn, client) -> None:
        panel = panel_slice(client.get(f"/source/{_mail(conn)}").text, "panel-reply")
        assert "nothing is sent" in panel


class TestDrafting:
    def test_a_draft_comes_back_as_a_fragment(self, conn, client, fake_model) -> None:
        fake_model(FakeClient())
        item = _mail(conn)
        page = client.post(f"/source/{item}/reply", data={"say": "ask for scope"})
        assert page.status_code == 200
        assert 'id="reply-draft"' in page.text
        assert "define the scope" in page.text

    def test_the_provenance_travels_with_the_draft(self, conn, client, fake_model) -> None:
        """Rule 1 on the reading side: the fragment carries what it read."""
        fake_model(FakeClient())
        item = _mail(conn)
        page = client.post(f"/source/{item}/reply", data={"say": "ask for scope"})
        assert "Where each part came from" in page.text
        assert f"#{item}" in page.text

    def test_a_missing_stance_renders_in_the_fragment_not_as_an_error(
        self, conn, client, fake_model
    ) -> None:
        """A 422 would be swallowed by the HTMX swap and the owner would see nothing."""
        fake_model(FakeClient())
        page = client.post(f"/source/{_mail(conn)}/reply", data={"say": "  "})
        assert page.status_code == 200
        assert "--say" in page.text

    def test_a_model_outage_degrades_to_a_sentence(self, conn, client, fake_model) -> None:
        """Rule 5 at the page level: it fails visibly and the page stays true."""
        fake_model(FakeClient(boom=True))
        page = client.post(f"/source/{_mail(conn)}/reply", data={"say": "ask for scope"})
        assert page.status_code == 200
        assert "model call failed" in page.text
        assert "retrying is safe" in page.text

    def test_a_watermarked_model_reply_is_cleaned_before_it_is_rendered(
        self, conn, client, fake_model
    ) -> None:
        # Scoped to the <pre> that holds the email itself. The evidence lines beneath it
        # are the app talking to the owner and follow the repo's prose style; the draft
        # is the only text that leaves the machine, and it is the only text held to this.
        fake_model(FakeClient({"subject": "Re: X", "body": "Dr. Shufeldt — I’d like to."}))
        page = client.post(f"/source/{_mail(conn)}/reply", data={"say": "yes"})
        drafted = _drafted_body(page.text)
        assert "—" not in drafted
        assert "’" not in drafted
        assert drafted == "Dr. Shufeldt - I&#39;d like to."

    def test_an_unfilled_placeholder_is_called_out(self, conn, client, fake_model) -> None:
        fake_model(FakeClient({"subject": "Re: X", "body": "Yes.\n\n[Your Name]"}))
        page = client.post(f"/source/{_mail(conn)}/reply", data={"say": "yes"})
        assert "Do not send it" in page.text


class TestItWritesNothing:
    def test_drafting_leaves_the_ledger_untouched(
        self, conn: sqlite3.Connection, client, fake_model
    ) -> None:
        """Rule 3. The page's own docstring claims read-only; this is the proof."""
        fake_model(FakeClient())
        item = _mail(conn)
        before = conn.execute("SELECT count(*) AS n FROM source_item").fetchone()["n"]
        for _ in range(2):
            client.post(f"/source/{item}/reply", data={"say": "ask for scope"})
        after = conn.execute("SELECT count(*) AS n FROM source_item").fetchone()["n"]
        assert before == after

    def test_no_send_endpoint_exists(self, conn, client) -> None:
        """The boundary, asserted rather than trusted: this app drafts and stops."""
        item = _mail(conn)
        for path in (f"/source/{item}/send", f"/source/{item}/reply/send"):
            assert client.post(path, data={}).status_code == 404
