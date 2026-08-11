"""Canvas connector. docs/07 §Canvas — the source that seeded the project and shipped
without a test.

Written on 2026-08-11, nine days before the semester it exists for. The connector was
complete and wired — config gate, detection, `_all_connectors`, docs row — and had no
`tests/test_canvas.py` at all, so nothing had ever exercised the field mapping that turns
an assignment into an obligation. That is the gap the sources skill's four-part gate names:
fixture-backed fetch, idempotency, health both ways, boundary.

Nothing here touches the network. The fake replaces `_get_url` only, so pagination, the
cursor watermark, the submission filter and the field mapping under test are all the real
code paths — the same arrangement tests/test_github.py uses for the same reason.
"""

from __future__ import annotations

import json
import urllib.error
from typing import Any

import pytest

from backglass.connectors import canvas as canvas_module
from backglass.connectors.base import Connector
from backglass.connectors.boundary import Boundary
from backglass.connectors.canvas import CanvasConnector

BASE = "https://asu.instructure.com"

#: RFC 5988, in the shape Canvas actually sends it.
NEXT = f'<{BASE}/api/v1/courses?page=2&per_page=100>; rel="next"'


@pytest.fixture
def permissive() -> Boundary:
    return Boundary(mode="full_scope")


@pytest.fixture
def enforcing() -> Boundary:
    return Boundary(mode="exclude", deny_domains=["clientexample.gov"])


class FakeCanvas(CanvasConnector):
    """Overrides only the HTTP layer. Pages are matched by substring, in order."""

    def __init__(self, pages: dict[str, tuple[Any, dict[str, str]]], **kwargs: Any):
        kwargs.setdefault("base_url", BASE)
        kwargs.setdefault("token", "t")
        super().__init__(**kwargs)
        self.pages = pages
        self.requested: list[str] = []

    def _get_url(self, url: str) -> tuple[Any, dict[str, str]]:
        self.requested.append(url)
        for key, value in self.pages.items():
            if key in url:
                return value
        return [], {}


def a_course(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": kwargs.pop("id", 101),
        "name": kwargs.pop("name", "BIO 181"),
        "account_id": kwargs.pop("account_id", "asu.edu"),
    }
    base.update(kwargs)
    return base


def an_assignment(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": kwargs.pop("id", 9001),
        "name": kwargs.pop("name", "Problem set 3"),
        "due_at": kwargs.pop("due_at", "2026-08-28T06:59:00Z"),
        "updated_at": kwargs.pop("updated_at", "2026-08-20T17:04:11Z"),
        "points_possible": kwargs.pop("points_possible", 25),
        "html_url": kwargs.pop("html_url", f"{BASE}/courses/101/assignments/9001"),
        "submission": kwargs.pop("submission", {}),
    }
    base.update(kwargs)
    return base


def one_course_one_assignment(**assignment: Any) -> dict[str, tuple[Any, dict[str, str]]]:
    return {
        "/api/v1/courses?": ([a_course()], {}),
        "/assignments": ([an_assignment(**assignment)], {}),
    }


# ── 1. fixture-backed fetch ───────────────────────────────────────────────


class TestAnAssignmentBecomesAnObligation:
    def test_the_due_date_survives_as_structure_not_prose(self, permissive: Boundary) -> None:
        """docs/07: emitted "with enough structure that extraction produces an `i_owe`
        without having to infer one from prose"."""
        connector = FakeCanvas(one_course_one_assignment(), boundary=permissive)

        items = list(connector.fetch(None))

        assert len(items) == 1
        item = items[0]
        assert item.source == "canvas:canvas"
        assert item.external_id == "101:9001"
        assert item.title == "BIO 181 — Problem set 3"
        assert "is due 2026-08-28T06:59:00+00:00" in item.body_text
        raw = json.loads(item.raw_json)
        assert raw["due_at"] == "2026-08-28T06:59:00Z"
        assert raw["points_possible"] == 25
        assert raw["html_url"].endswith("/assignments/9001")

    def test_an_assignment_with_no_due_date_is_not_an_obligation(
        self, permissive: Boundary
    ) -> None:
        connector = FakeCanvas(one_course_one_assignment(due_at=None), boundary=permissive)
        assert list(connector.fetch(None)) == []

    @pytest.mark.parametrize(
        "submission",
        [
            {"submitted_at": "2026-08-27T22:10:00Z"},
            {"workflow_state": "graded"},
        ],
    )
    def test_already_delivered_work_is_not_an_open_obligation(
        self, permissive: Boundary, submission: dict[str, Any]
    ) -> None:
        """The whole reason `include[]=submission` is on the request. Without this the
        ledger fills with work already handed in, and every one of them reads as overdue."""
        connector = FakeCanvas(
            one_course_one_assignment(submission=submission), boundary=permissive
        )
        assert list(connector.fetch(None)) == []

    def test_pagination_follows_link_headers_to_completion(
        self, permissive: Boundary
    ) -> None:
        connector = FakeCanvas(
            {
                "/api/v1/courses?enrollment_state": ([a_course()], {"Link": NEXT}),
                "/api/v1/courses?page=2": ([a_course(id=102, name="CHM 113")], {}),
                "/courses/101/assignments": ([an_assignment()], {}),
                "/courses/102/assignments": ([an_assignment(id=9002, name="Lab report")], {}),
            },
            boundary=permissive,
        )

        titles = [item.title for item in connector.fetch(None)]

        assert titles == ["BIO 181 — Problem set 3", "CHM 113 — Lab report"]

    def test_the_cursor_advances_to_the_newest_update(self, permissive: Boundary) -> None:
        connector = FakeCanvas(
            {
                "/api/v1/courses?": ([a_course()], {}),
                "/assignments": (
                    [
                        an_assignment(id=1, updated_at="2026-08-20T17:04:11Z"),
                        an_assignment(id=2, updated_at="2026-08-22T09:00:00Z"),
                    ],
                    {},
                ),
            },
            boundary=permissive,
        )

        list(connector.fetch(None))

        assert connector.cursor == "2026-08-22T09:00:00+00:00"

    def test_an_item_at_the_watermark_is_re_read_rather_than_lost(
        self, permissive: Boundary
    ) -> None:
        """The connector's own reasoning: Canvas's `updated_at` is mutable and
        whole-second, so a bulk administrative edit can stamp several assignments with the
        second already stored. Re-reading costs zero writes; dropping loses them."""
        stamp = "2026-08-20T17:04:11+00:00"
        connector = FakeCanvas(one_course_one_assignment(), boundary=permissive)

        assert len(list(connector.fetch(stamp))) == 1

    def test_an_older_item_is_skipped(self, permissive: Boundary) -> None:
        connector = FakeCanvas(one_course_one_assignment(), boundary=permissive)
        assert list(connector.fetch("2026-09-01T00:00:00+00:00")) == []


# ── 2. idempotency ────────────────────────────────────────────────────────


def test_two_runs_over_unchanged_data_produce_identical_hashes(
    permissive: Boundary,
) -> None:
    """The change primitive. `content_hash` must be a function of the assignment alone —
    a run-time value anywhere in it makes every sync rewrite every row."""
    first = list(FakeCanvas(one_course_one_assignment(), boundary=permissive).fetch(None))
    second = list(FakeCanvas(one_course_one_assignment(), boundary=permissive).fetch(None))

    assert [i.content_hash for i in first] == [i.content_hash for i in second]
    assert [i.external_id for i in first] == [i.external_id for i in second]


def test_a_changed_due_date_changes_the_hash(permissive: Boundary) -> None:
    """The other half: idempotent must not mean blind. A moved deadline is the single most
    important thing this source can tell the owner."""
    before = list(FakeCanvas(one_course_one_assignment(), boundary=permissive).fetch(None))
    after = list(
        FakeCanvas(
            one_course_one_assignment(
                due_at="2026-08-30T06:59:00Z", updated_at="2026-08-25T12:00:00Z"
            ),
            boundary=permissive,
        ).fetch(None)
    )

    assert before[0].external_id == after[0].external_id
    assert before[0].content_hash != after[0].content_hash


# ── 3. health, both ways ──────────────────────────────────────────────────


class TestHealthSaysWhichFailureThisIs:
    def test_an_unset_gate_says_so_rather_than_calling_out(self) -> None:
        connector = CanvasConnector(base_url="", token="", boundary=Boundary(mode="full_scope"))
        health = connector.health()
        assert not health.ok
        assert "CANVAS_BASE_URL" in health.detail

    def test_a_working_token_is_healthy(self, permissive: Boundary) -> None:
        connector = FakeCanvas({"/users/self": ({"id": 7}, {})}, boundary=permissive)
        assert connector.health().ok

    def test_a_rejected_token_names_the_documented_cause(
        self, permissive: Boundary, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """docs/07's warning, and the one this owner is most likely to hit: some
        institutions disable student-generated tokens entirely. Read as a transient error
        it would look like a flaky sync forever."""

        def raise_401(*args: Any, **kwargs: Any) -> Any:
            raise urllib.error.HTTPError(
                f"{BASE}/api/v1/users/self", 401, "Unauthorized", {}, None  # type: ignore[arg-type]
            )

        monkeypatch.setattr(canvas_module.urllib.request, "urlopen", raise_401)
        connector = CanvasConnector(
            base_url=BASE, token="bad", boundary=permissive, timeout_seconds=1
        )

        health = connector.health()

        assert not health.ok
        assert "Approved Integrations" in health.detail
        assert "ICS feed" in health.detail

    def test_a_failing_health_never_leaks_the_token(
        self, permissive: Boundary, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`base.safe_error` exists because a raw `str(exc)` on a URL request carries the
        request — and this connector's request carries a bearer token."""

        def raise_boom(*args: Any, **kwargs: Any) -> Any:
            raise urllib.error.URLError("Bearer sekrit-token-value failed")

        monkeypatch.setattr(canvas_module.urllib.request, "urlopen", raise_boom)
        connector = CanvasConnector(
            base_url=BASE, token="sekrit-token-value", boundary=permissive, timeout_seconds=1
        )

        assert "sekrit-token-value" not in connector.health().detail


# ── 4. boundary ───────────────────────────────────────────────────────────


def test_an_excluded_course_is_counted_and_never_stored(enforcing: Boundary) -> None:
    """docs/08 D1: the check runs before persistence, and an excluded item leaves a tally
    rather than a row."""
    connector = FakeCanvas(
        {
            "/api/v1/courses?": ([a_course(account_id="clientexample.gov")], {}),
            "/assignments": ([an_assignment()], {}),
        },
        boundary=enforcing,
    )

    items = list(connector.fetch(None))

    assert items == []
    assert connector.excluded == 1
    assert sum(connector.excluded_by_rule.values()) == 1


def test_an_allowed_course_is_not_counted_as_excluded(enforcing: Boundary) -> None:
    connector = FakeCanvas(one_course_one_assignment(), boundary=enforcing)
    assert len(list(connector.fetch(None))) == 1
    assert connector.excluded == 0


# ── the protocol ──────────────────────────────────────────────────────────


def test_it_satisfies_the_connector_protocol(permissive: Boundary) -> None:
    connector = CanvasConnector(base_url=BASE, token="t", boundary=permissive)
    assert isinstance(connector, Connector)
    assert connector.name == "canvas:canvas"
