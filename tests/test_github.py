"""GitHub connector. docs/07 §Connectors, applied to a source whose obligations arrive
already structured.

Nothing here touches the network: the fake replaces `_get_url` only, so pagination, the
cursor watermark, the rate-limit stop and the field mapping under test are all the real
code paths.
"""

from __future__ import annotations

import email.message
import json
import urllib.error
from typing import Any

import pytest

from backglass.connectors import github as github_module
from backglass.connectors.base import Connector
from backglass.connectors.boundary import Boundary
from backglass.connectors.github import GithubConnector

NEXT = '<https://api.github.com/search/issues?q=involves%3A%40me&page=2>; rel="next"'


@pytest.fixture
def enforcing() -> Boundary:
    return Boundary(mode="exclude", deny_domains=["clientexample.gov"])


# ── fakes ─────────────────────────────────────────────────────────────────


class FakeGithub(GithubConnector):
    """Overrides only the HTTP layer. Pages are matched by substring, in order."""

    def __init__(self, pages: dict[str, tuple[Any, dict[str, str]]], **kwargs: Any):
        super().__init__(token="t", **kwargs)
        self.pages = pages
        self.requested: list[str] = []

    def _get_url(self, url: str) -> tuple[Any, dict[str, str]]:
        self.requested.append(url)
        for key, value in self.pages.items():
            if key in url:
                return value
        return {"items": []}, {}


def an_issue(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "number": kwargs.pop("number", 41),
        "title": kwargs.pop("title", "Ledger dedupe drops supersedes"),
        "state": kwargs.pop("state", "open"),
        "body": kwargs.pop("body", "Fix before the Friday cut."),
        "updated_at": kwargs.pop("updated_at", "2026-07-28T09:15:32Z"),
        "user": {"login": kwargs.pop("author", "dana")},
        "repository_url": "https://api.github.com/repos/kesavan/backglass",
        "labels": kwargs.pop("labels", [{"name": "bug"}]),
        "assignees": kwargs.pop("assignees", [{"login": "kesavan"}]),
    }
    base.update(kwargs)
    return base


def one_page(issues: list[dict[str, Any]], **headers: str) -> dict[str, Any]:
    return {"/search/issues": ({"items": issues}, headers)}


# ── mapping ───────────────────────────────────────────────────────────────


def test_an_assigned_issue_becomes_a_source_item(enforcing: Boundary) -> None:
    connector = FakeGithub(one_page([an_issue()]), boundary=enforcing)
    items = list(connector.fetch(None))

    assert len(items) == 1
    item = items[0]
    assert item.source == "github"
    assert item.external_id == "kesavan/backglass#41"
    assert item.title == "[kesavan/backglass] Ledger dedupe drops supersedes"
    assert item.author == "dana"
    assert "issue, open" in str(item.body_text)
    assert "Assigned to: kesavan" in str(item.body_text)
    assert "bug" in str(item.body_text)
    assert "Friday cut" in str(item.body_text)
    assert json.loads(item.raw_json)["number"] == 41
    assert item.content_hash


def test_occurred_at_is_the_issue_update_not_the_run(enforcing: Boundary) -> None:
    """CLAUDE.md rule 4. "Before the Friday cut" on a three-week-old issue is not this
    Friday, so the timestamp every relative date resolves against is `updated_at`."""
    connector = FakeGithub(
        one_page([an_issue(updated_at="2026-07-08T22:04:05Z")]), boundary=enforcing
    )
    item = next(iter(connector.fetch(None)))
    assert item.occurred_at == "2026-07-08T22:04:05+00:00"


def test_a_pull_request_is_labelled_as_one(enforcing: Boundary) -> None:
    connector = FakeGithub(
        one_page([an_issue(number=7, pull_request={"url": "..."})]), boundary=enforcing
    )
    item = next(iter(connector.fetch(None)))
    assert "pull request" in str(item.body_text)
    assert item.external_id == "kesavan/backglass#7"


def test_a_long_body_is_truncated(enforcing: Boundary) -> None:
    """docs/02's cost model. The obligation is in the first screenful; the rest is a
    thread the extractor should not be charged for."""
    connector = FakeGithub(one_page([an_issue(body="x" * 9000)]), boundary=enforcing)
    item = next(iter(connector.fetch(None)))
    assert str(item.body_text).count("x") == 4000


# ── pagination ────────────────────────────────────────────────────────────


def test_pagination_follows_link_to_completion(enforcing: Boundary) -> None:
    """docs/07: follow RFC 5988 Link headers to completion, as Canvas does."""
    connector = FakeGithub(
        {
            "page=2": ({"items": [an_issue(number=2, updated_at="2026-07-29T00:00:00Z")]}, {}),
            "/search/issues": ({"items": [an_issue(number=1)]}, {"Link": NEXT}),
        },
        boundary=enforcing,
    )
    numbers = [item.external_id for item in connector.fetch(None)]
    assert numbers == ["kesavan/backglass#1", "kesavan/backglass#2"]
    assert any("page=2" in url for url in connector.requested)


# ── the cursor ────────────────────────────────────────────────────────────


def test_the_cursor_advances_to_the_newest_update_at_full_precision(
    enforcing: Boundary,
) -> None:
    """tasks/lessons.md 2026-07-30: a watermark truncated to whole seconds re-reads the
    whole source every run, and idempotency hides it because nothing is written."""
    connector = FakeGithub(
        one_page(
            [
                an_issue(number=1, updated_at="2026-07-28T09:15:32Z"),
                an_issue(number=2, updated_at="2026-07-29T11:02:07Z"),
            ]
        ),
        boundary=enforcing,
    )
    list(connector.fetch(None))
    assert connector.cursor == "2026-07-29T11:02:07+00:00"


def test_a_second_run_asks_only_for_newer_issues(enforcing: Boundary) -> None:
    """CLAUDE.md rule 3. The watermark goes into the query, so the second run does not
    re-fetch what the first one already read — and re-yields nothing if it does."""
    pages = one_page(
        [
            an_issue(number=1, updated_at="2026-07-28T09:15:32Z"),
            an_issue(number=2, updated_at="2026-07-29T11:02:07Z"),
        ]
    )
    first = FakeGithub(pages, boundary=enforcing)
    assert len(list(first.fetch(None))) == 2
    cursor = first.cursor
    assert cursor

    second = FakeGithub(pages, boundary=enforcing)
    assert list(second.fetch(cursor)) == [], "an unchanged issue is not read twice"
    assert "updated%3A%3E2026-07-29T11%3A02%3A07%2B00%3A00" in second.requested[0]
    assert "involves%3A%40me" in second.requested[0]
    assert "order=asc" in second.requested[0], (
        "ascending order is what makes an early stop resumable"
    )


# ── quota ─────────────────────────────────────────────────────────────────


def test_an_exhausted_rate_limit_stops_cleanly(enforcing: Boundary) -> None:
    """A spent quota is not a failure; it is a partial run. Return what was fetched with
    a cursor the next run can resume from, and never follow the next link."""
    connector = FakeGithub(
        {
            "page=2": ({"items": [an_issue(number=2)]}, {}),
            "/search/issues": (
                {"items": [an_issue(number=1, updated_at="2026-07-28T09:15:32Z")]},
                {"Link": NEXT, "X-RateLimit-Remaining": "0"},
            ),
        },
        boundary=enforcing,
    )
    items = list(connector.fetch(None))

    assert len(items) == 1
    assert connector.rate_limited is True
    assert connector.cursor == "2026-07-28T09:15:32+00:00"
    assert not any("page=2" in url for url in connector.requested)


# ── health ────────────────────────────────────────────────────────────────


def test_a_revoked_token_is_a_health_state_not_a_crash(enforcing: Boundary) -> None:
    class Rejecting(FakeGithub):
        def _get_url(self, url: str) -> tuple[Any, dict[str, str]]:
            raise PermissionError("HTTP 401 Unauthorized")

    health = Rejecting({}, boundary=enforcing).health()
    assert health.ok is False
    assert "401" in str(health.detail)
    assert "revoked or expired" in str(health.detail)


def test_a_401_from_the_api_is_translated_before_health_sees_it(
    enforcing: Boundary, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same claim, with the real HTTP layer in the loop: `urlopen`
    is the only thing replaced, so the 401 → PermissionError translation is exercised
    rather than assumed. No socket is opened."""

    def raise_401(request: Any, timeout: int = 0) -> Any:
        del request, timeout
        raise urllib.error.HTTPError(
            "https://api.github.com/user", 401, "Unauthorized", email.message.Message(), None
        )

    monkeypatch.setattr(github_module.urllib.request, "urlopen", raise_401)
    health = GithubConnector(token="revoked", boundary=enforcing).health()
    assert health.ok is False
    assert "revoked or expired" in str(health.detail)
    assert "revoked" not in str(health.detail).replace("revoked or expired", ""), (
        "the token itself never reaches a health string"
    )


def test_a_missing_token_is_a_health_state(enforcing: Boundary) -> None:
    assert GithubConnector(token="", boundary=enforcing).health().ok is False


def test_a_healthy_token_reports_ok(enforcing: Boundary) -> None:
    connector = FakeGithub({"/user": ({"login": "kesavan"}, {})}, boundary=enforcing)
    assert connector.health().ok is True


# ── the boundary ──────────────────────────────────────────────────────────


def test_an_issue_quoting_a_client_address_is_excluded(enforcing: Boundary) -> None:
    """docs/08 D1 applies here too. GitHub identities are logins, so the only way a client
    address reaches the ledger through this source is prose someone pasted in."""
    connector = FakeGithub(
        one_page([an_issue(body="Forwarding from dana@clientexample.gov — the WIC numbers")]),
        boundary=enforcing,
    )
    assert list(connector.fetch(None)) == []
    assert connector.excluded == 1
    assert connector.excluded_by_rule == {"clientexample.gov": 1}


def test_it_satisfies_the_connector_protocol() -> None:
    connector = GithubConnector(token="t", boundary=Boundary(mode="full_scope"))
    assert isinstance(connector, Connector)
    assert connector.name == "github"
