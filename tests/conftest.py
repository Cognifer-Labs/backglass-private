"""Shared test doubles.

docs/10 §Testing: "No test calls a live third-party API." Two doubles do all the work.

`FakeGmailService` mimics the chained googleapiclient surface the connector actually
touches. The plan called for vcrpy cassettes; they cannot be recorded until the owner has
run the OAuth consent flow, so the fixtures are hand-written against the documented
`users.messages.get` response shape instead. Recording real cassettes is listed as
outstanding in tasks/todo.md.

`FakeModel` returns canned responses keyed by source item. docs/10: "model responses are
hand-written JSON so they stay readable" — these test the post-processing in
extract-commitments.md §Post-processing, not the model.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from backglass.config import Settings
from backglass.connectors.boundary import Boundary
from backglass.connectors.gmail import GmailConnector
from backglass.db import connect, migrate
from backglass.extract.client import ModelResult

FIXTURES = Path(__file__).parent / "fixtures"


# ────────────────────────────────────────────────────────── message fixtures


def gmail_message(spec: dict[str, Any]) -> dict[str, Any]:
    """Build a Gmail API `users.messages.get(format='full')` response from a compact spec.

    Written as a builder rather than checked-in base64 so the fixtures stay readable —
    a reviewer can see the headers a boundary test depends on without decoding anything.
    """
    headers = [
        {"name": name, "value": value}
        for name, value in (
            ("From", spec.get("from")),
            ("To", spec.get("to")),
            ("Cc", spec.get("cc")),
            ("Bcc", spec.get("bcc")),
            ("Subject", spec.get("subject")),
            ("Date", spec.get("date")),
            ("Content-Type", spec.get("content_type")),
            ("List-Unsubscribe", spec.get("list_unsubscribe")),
            ("Precedence", spec.get("precedence")),
        )
        if value
    ]
    body = spec.get("body", "")
    return {
        "id": spec["id"],
        "threadId": spec.get("thread_id", spec["id"]),
        "labelIds": spec.get("label_ids", ["INBOX"]),
        "internalDate": spec.get("internal_date", "1752163200000"),
        "payload": {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
        },
    }


def load_specs(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / name).read_text())


# ─────────────────────────────────────────────────────────────── the doubles


class _Request:
    def __init__(self, payload: Any):
        self._payload = payload

    def execute(self) -> Any:
        return self._payload


class _Messages:
    def __init__(self, service: FakeGmailService):
        self._service = service

    def list(self, **kwargs: Any) -> _Request:
        del kwargs
        return _Request({"messages": [{"id": m["id"]} for m in self._service.messages]})

    def list_next(self, request: Any, response: Any) -> None:
        del request, response
        return None

    def get(self, *, userId: str, id: str, format: str) -> _Request:  # noqa: N803, A002
        del userId, format
        self._service.get_calls += 1
        for message in self._service.messages:
            if message["id"] == id:
                return _Request(message)
        raise KeyError(id)


class _History:
    def __init__(self, service: FakeGmailService):
        self._service = service

    def list(self, **kwargs: Any) -> _Request:
        if self._service.history_raises:
            raise RuntimeError("historyId is too old")
        del kwargs
        return _Request(
            {
                "historyId": self._service.history_id,
                "history": [
                    {"messagesAdded": [{"message": {"id": m["id"]}}]}
                    for m in self._service.messages
                ],
            }
        )

    def list_next(self, request: Any, response: Any) -> None:
        del request, response
        return None


class _Users:
    def __init__(self, service: FakeGmailService):
        self._service = service

    def messages(self) -> _Messages:
        return _Messages(self._service)

    def history(self) -> _History:
        return _History(self._service)

    def getProfile(self, **kwargs: Any) -> _Request:  # noqa: N802
        del kwargs
        if self._service.profile_raises:
            raise RuntimeError("invalid_grant: Token has been expired or revoked.")
        return _Request(
            {"emailAddress": "owner@example.com", "historyId": self._service.history_id}
        )


class FakeGmailService:
    def __init__(self, messages: list[dict[str, Any]], *, history_id: str = "99001"):
        self.messages = messages
        self.history_id = history_id
        self.get_calls = 0
        self.profile_raises = False
        self.history_raises = False

    def users(self) -> _Users:
        return _Users(self)


class FakeModel:
    """Returns a canned response, selected by a marker appearing in the rendered prompt.

    Which tier is being called is read off the schema rather than the model name, because
    the schema is generated from the pydantic model and cannot drift from what the caller
    will try to validate.
    """

    def __init__(
        self,
        extract: dict[str, dict[str, Any]] | None = None,
        triage: dict[str, dict[str, Any]] | None = None,
        *,
        triage_batch: dict[str, dict[str, Any]] | None = None,
        cost_usd: float = 0.001,
    ):
        # Keyed per tier, not per marker alone: the same subject line appears in both the
        # triage prompt and the extraction prompt, so a single marker table would hand a
        # triage call an extraction-shaped response and fail validation.
        self.extract = extract or {}
        self.triage = triage or {}
        self.triage_batch = triage_batch or {}
        self.cost_usd = cost_usd
        self.calls: list[tuple[str, str]] = []

    def complete(
        self, *, system: str, user: str, schema: dict[str, Any], model: str, budget_usd: float
    ) -> ModelResult:
        del system, budget_usd
        props = schema.get("properties", {})
        if "keep" in props:
            tier = "triage"
        elif "items" in props:
            tier = "triage_batch"
        else:
            tier = "extract"
        self.calls.append((tier, model))
        table = {
            "triage": self.triage,
            "triage_batch": self.triage_batch,
            "extract": self.extract,
        }[tier]
        for key, response in table.items():
            if key in user:
                return ModelResult(data=response, cost_usd=self.cost_usd)
        default: dict[str, Any]
        if tier == "triage":
            # triage.md: "When genuinely uncertain, return keep=true."
            default = {"keep": True, "reason": "no fixture matched; defaulting to keep"}
        elif tier == "triage_batch":
            # An empty batch response escalates every item to the per-item pass, where
            # the existing `triage` fixtures apply — batch-unaware tests keep working.
            default = {"items": []}
        else:
            default = {"commitments": []}
        return ModelResult(data=default, cost_usd=self.cost_usd)


# ──────────────────────────────────────────────────────────────── fixtures


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # Behavior-shaping knobs are pinned to the documented defaults so the suite does
    # not inherit the owner's live .env — personalizing WORKING_WINDOW must not move
    # planner fixtures. Env *parsing* stays covered by tests/test_config.py, which
    # reads the environment on purpose (the 2026-07-30 lesson).
    return Settings(
        owner_name="K",
        owner_emails=["contactdharsan@gmail.com", "dkesava2@asu.edu"],
        boundary_mode="full_scope",
        db_path=tmp_path / "backglass.db",
        model_backend="claude_cli",
        max_concurrency=1,
        working_window="09:00-18:00",
        peak_window="09:00-12:00",
        default_tz="America/Phoenix",
        alt_tz="Asia/Kolkata",
        tz_ranges=[],
        noise_senders=[],
    )


@pytest.fixture
def conn(settings: Settings):  # type: ignore[no-untyped-def]
    connection = connect(settings.db_path)
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def boundary(settings: Settings) -> Boundary:
    return Boundary.from_settings(settings)


def make_connector(
    messages: list[dict[str, Any]], boundary: Boundary, *, label: str = "personal"
) -> GmailConnector:
    return GmailConnector(label=label, service=FakeGmailService(messages), boundary=boundary)
