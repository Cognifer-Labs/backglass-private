"""AnthropicAPIBackend: forced tool use, caching markers, real-usage cost, budget.

No live API — the SDK client is injected (same pattern as AppleShortcutBackend.runner).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from backglass.extract import pricing, prompts
from backglass.extract.client import SYSTEM, AnthropicAPIBackend, ModelError

TRIAGE_SCHEMA = {"type": "object", "properties": {"keep": {}, "reason": {}}}
EXTRACT_SCHEMA = {"type": "object", "properties": {"commitments": {}}}


def _usage(
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )


def _response(data: Any, usage: SimpleNamespace | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name="emit", input=data)],
        usage=usage or _usage(100, 10),
    )


class FakeAnthropic:
    """Canned responses; records every params dict for assertions."""

    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **params: Any) -> Any:
        self.requests.append(params)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _backend(responses: list[Any]) -> tuple[AnthropicAPIBackend, FakeAnthropic]:
    fake = FakeAnthropic(responses)
    return AnthropicAPIBackend(api_key="k", client=fake), fake


class TestRequestShape:
    def test_forced_tool_use_and_cache_marker(self) -> None:
        backend, fake = _backend([_response({"keep": True, "reason": "ask"})])
        result = backend.complete(
            system="sys", user="msg", schema=TRIAGE_SCHEMA, model="haiku", budget_usd=1.0
        )

        assert result.data == {"keep": True, "reason": "ask"}
        request = fake.requests[0]
        assert request["model"] == "claude-haiku-4-5", "alias resolved"
        assert request["tool_choice"] == {"type": "tool", "name": "emit"}
        assert request["tools"][0]["input_schema"] == TRIAGE_SCHEMA
        assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert request["max_tokens"] == backend.max_tokens_triage

    def test_concrete_model_id_passes_through_and_extract_budget(self) -> None:
        backend, fake = _backend([_response({"commitments": []})])
        backend.complete(
            system="s", user="u", schema=EXTRACT_SCHEMA,
            model="claude-sonnet-4-6", budget_usd=1.0,
        )
        assert fake.requests[0]["model"] == "claude-sonnet-4-6"
        assert fake.requests[0]["max_tokens"] == backend.max_tokens_extract


class TestCost:
    def test_cost_is_exact_usage_arithmetic(self) -> None:
        usage = _usage(10_000, 200, cache_read=50_000, cache_write=2_000)
        backend, _ = _backend([_response({"keep": True, "reason": "r"}, usage)])
        result = backend.complete(
            system="s", user="u", schema=TRIAGE_SCHEMA, model="haiku", budget_usd=1.0
        )
        # haiku: in $1, out $5, cache-read $0.10, cache-write $1.25 per MTok.
        expected = (10_000 * 1.0 + 200 * 5.0 + 50_000 * 0.10 + 2_000 * 1.25) / 1e6
        assert result.cost_usd == pytest.approx(expected)

    def test_unpriced_model_raises_never_bills_zero(self) -> None:
        with pytest.raises(ModelError, match="no price on record"):
            pricing.cost_usd("claude-mystery-9", _usage(1, 1))


class TestBudgetGuard:
    def test_absurd_budget_parks_before_any_call(self) -> None:
        backend, fake = _backend([_response({"keep": True, "reason": "r"})])
        with pytest.raises(ModelError, match="per-call budget"):
            backend.complete(
                system="s" * 4000, user="u" * 4000, schema=TRIAGE_SCHEMA,
                model="haiku", budget_usd=0.0001,
            )
        assert fake.requests == [], "the guard must fire before any spend"


class TestRetries:
    def test_malformed_tool_result_is_restated_once(self) -> None:
        no_tool = SimpleNamespace(content=[], usage=_usage(10, 1))
        backend, fake = _backend([no_tool, _response({"keep": False, "reason": "r"})])
        result = backend.complete(
            system="s", user="u", schema=TRIAGE_SCHEMA, model="haiku", budget_usd=1.0
        )
        assert result.data["keep"] is False
        assert len(fake.requests) == 2
        assert "did not match the required schema" in fake.requests[1]["messages"][0]["content"]
        assert result.cost_usd > 0, "the failed attempt's cost is accumulated"

    def test_sdk_error_becomes_model_error(self) -> None:
        backend, _ = _backend([RuntimeError("connection reset"), RuntimeError("again")])
        with pytest.raises(ModelError, match="unusable response after 2 attempts"):
            backend.complete(
                system="s", user="u", schema=TRIAGE_SCHEMA, model="haiku", budget_usd=1.0
            )


class TestPromptSplit:
    """The caching refactor must not change a single byte of total prompt content."""

    @pytest.mark.parametrize("name", ["triage", "triage-batch", "extract-commitments"])
    def test_static_plus_dynamic_equals_render(self, name: str) -> None:
        prompt = prompts.load(name)
        static, dynamic = prompt.split()
        values = {p: f"<{p}>" for p in prompt.placeholders()}
        joined = (static + "\n" if static else "") + prompt.render_dynamic(**values)
        assert joined == prompt.render(**values)

    def test_all_placeholders_land_in_the_dynamic_half(self) -> None:
        for name in ("triage", "triage-batch", "extract-commitments"):
            prompt = prompts.load(name)
            static, _ = prompt.split()
            assert not prompts._PLACEHOLDER.search(static), name

    def test_system_carries_the_static_instructions(self) -> None:
        prompt = prompts.load("triage")
        static, _ = prompt.split()
        assert "You are triaging one message" in static
        assert SYSTEM  # and the backend-level system prompt still exists to prefix it
