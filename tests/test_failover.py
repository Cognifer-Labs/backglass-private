"""The provider chain: free first, Anthropic when free will not serve.

The owner's instruction on 2026-08-24 was "use whatever is free, then fall back on
Anthropic models". Until then the only failover in the model layer was a *model* chain —
`DeepInfraBackend.fallbacks` hands OpenRouter a `models` array — which is one gateway
trying several models and cannot help when the gateway itself is what refuses. On a free
tier the gateway is exactly what refuses: OpenRouter allows 50 requests a day against an
arrival rate measured at 50–276 items a day.

Two of these tests are about money rather than routing, and they are the ones worth
reading twice. A `claude_cli` secondary reports an API-equivalent *price* for a call
nobody was billed for, and adding that to the monthly cap is the 2026-08-03 failure where
nine consecutive syncs degraded to triage-only over $20.06 that never existed.
"""

from __future__ import annotations

from typing import Any

import pytest

from backglass.config import Settings
from backglass.extract.client import (
    AuthCircuit,
    Failover,
    ModelAuthError,
    ModelError,
    ModelResult,
    RateLimited,
    build,
)

SCHEMA: dict[str, Any] = {"type": "object", "properties": {"keep": {}, "reason": {}}}


class Recorder:
    """A backend that records what it was asked and answers however it was told to."""

    def __init__(
        self,
        *,
        raises: Exception | None = None,
        cost: float = 0.0,
        imputed: bool = False,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.raises = raises
        self.cost = cost
        self.spend_is_imputed = imputed
        self.data = data if data is not None else {"keep": True, "reason": "r"}
        self.models: list[str] = []

    def complete(
        self, *, system: str, user: str, schema: dict[str, Any], model: str, budget_usd: float
    ) -> ModelResult:
        del system, user, schema, budget_usd
        self.models.append(model)
        if self.raises is not None:
            raise self.raises
        return ModelResult(data=self.data, cost_usd=self.cost)


def a_chain(primary: Recorder, secondary: Recorder, **kwargs: Any) -> Failover:
    return Failover(
        primary=primary,
        secondary=secondary,
        remap={"nvidia/nemotron-nano-9b-v2:free": "haiku", "big/model:free": "sonnet"},
        default_model="sonnet",
        **kwargs,
    )


def call(client: Any, model: str = "nvidia/nemotron-nano-9b-v2:free") -> ModelResult:
    return client.complete(system="s", user="u", schema=SCHEMA, model=model, budget_usd=0.1)


# ── crossing over ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "failure",
    [
        RateLimited("model rate limit: HTTP 429: free-models-per-day"),
        ModelAuthError("authentication failed: HTTP 401"),
        ModelError("deepinfra response contained no usable tool call"),
    ],
    ids=["shut window", "dead credentials", "no usable tool call"],
)
def test_every_way_the_free_provider_can_refuse_crosses_over(failure: Exception) -> None:
    """All three mean "the free provider did not answer", which is the whole condition
    the owner named. A chain that only catches one of them is a chain that is off on the
    days the other two happen."""
    primary = Recorder(raises=failure)
    secondary = Recorder()
    result = call(a_chain(primary, secondary))

    assert result.data == {"keep": True, "reason": "r"}
    assert secondary.models == ["haiku"]


def test_a_working_primary_is_never_second_guessed() -> None:
    primary = Recorder(data={"keep": False, "reason": "noise"})
    secondary = Recorder()
    chain = a_chain(primary, secondary)

    assert call(chain).data == {"keep": False, "reason": "noise"}
    assert secondary.models == [], "the paid path is not touched when free works"
    assert chain.used_secondary == 0


def test_the_secondarys_own_failure_propagates() -> None:
    """There is nowhere further to go, and swallowing it would turn a real outage into
    silence — the item must still land in the run's errors and stay pending."""
    chain = a_chain(
        Recorder(raises=RateLimited("shut")), Recorder(raises=ModelError("cli is gone"))
    )
    with pytest.raises(ModelError, match="cli is gone"):
        call(chain)


# ── the model name does not survive the crossing ──────────────────────────


@pytest.mark.parametrize(
    ("asked", "expected"),
    [
        ("nvidia/nemotron-nano-9b-v2:free", "haiku"),
        ("big/model:free", "sonnet"),
        ("something/nobody/mapped", "sonnet"),
    ],
    ids=["triage", "extract", "unmapped falls to the capable one"],
)
def test_the_model_is_remapped_for_the_secondary(asked: str, expected: str) -> None:
    """`MODEL_TRIAGE=nvidia/nemotron-nano-9b-v2:free` means nothing to the Claude CLI.
    Passing it straight through is a call that fails on arrival, every time, for the whole
    duration of the outage the fallback exists to cover."""
    secondary = Recorder()
    call(a_chain(Recorder(raises=RateLimited("shut")), secondary), model=asked)
    assert secondary.models == [expected]


# ── money ─────────────────────────────────────────────────────────────────


def test_an_imputed_secondarys_price_is_not_charged_to_the_cap() -> None:
    """tasks/lessons.md 2026-08-03. `claude_cli` reports an API-equivalent price for a
    subscription call nobody was billed for; summing it into `monthly_spend_cap_cents`
    degraded nine consecutive syncs to triage-only over money that did not exist. The
    fallback path is a new way to reach the same mistake."""
    secondary = Recorder(cost=0.42, imputed=True)
    result = call(a_chain(Recorder(raises=RateLimited("shut")), secondary))
    assert result.cost_usd == 0.0


def test_a_billed_secondarys_cost_is_charged() -> None:
    """The other half. Zeroing every fallback cost would make the cap guard nothing the
    moment the secondary is a real API key."""
    secondary = Recorder(cost=0.42, imputed=False)
    result = call(a_chain(Recorder(raises=RateLimited("shut")), secondary))
    assert result.cost_usd == 0.42


def test_the_chain_reports_the_primarys_billing_answer() -> None:
    """`spend_is_imputed` is asked by the cap, `costs.py` and the dashboard. The primary
    is where billing can actually happen, so it is the primary's answer."""
    chain = a_chain(Recorder(imputed=False), Recorder(imputed=True))
    assert chain.spend_is_imputed is False
    assert chain.secondary_is_imputed is True


def test_the_chain_says_when_it_carried_the_run() -> None:
    """A chain silently carrying every call is a chain nobody knows they depend on."""
    chain = a_chain(Recorder(raises=RateLimited("HTTP 429: free-models-per-day")), Recorder())
    call(chain)
    call(chain)
    assert chain.used_secondary == 2
    assert "free-models-per-day" in chain.first_reason


# ── where it sits in the stack ────────────────────────────────────────────


def test_a_primary_auth_failure_does_not_trip_the_circuit_when_the_chain_recovers() -> None:
    """`Failover` goes *inside* `AuthCircuit`, and the order is the point.

    The circuit stops a run from spending calls on credentials already rejected. That
    verdict belongs to the chain as a whole: a dead OpenRouter key is not a reason to stop
    when the secondary is a working CLI session. Wrapped the other way round, the first
    401 would trip the circuit and every later item would be refused from memory without
    ever reaching the backend that would have answered.
    """
    secondary = Recorder()
    guarded = AuthCircuit(
        a_chain(Recorder(raises=ModelAuthError("authentication failed")), secondary)
    )
    for _ in range(3):
        assert call(guarded).data == {"keep": True, "reason": "r"}
    assert secondary.models == ["haiku", "haiku", "haiku"], "no item answered from memory"


def test_the_circuit_still_trips_when_the_whole_chain_is_rejected() -> None:
    guarded = AuthCircuit(
        a_chain(
            Recorder(raises=ModelAuthError("primary rejected")),
            Recorder(raises=ModelAuthError("secondary rejected")),
        )
    )
    with pytest.raises(ModelAuthError):
        call(guarded)
    with pytest.raises(ModelAuthError, match="earlier in this run"):
        call(guarded)


# ── construction ──────────────────────────────────────────────────────────


def _settings(**kwargs: Any) -> Settings:
    base = {
        "owner_name": "T",
        "model_backend": "openai_compatible",
        "model_base_url": "https://openrouter.ai/api/v1",
        "model_api_key": "k",
        "model_triage": "free/triage:free",
        "model_extract": "free/extract:free",
    }
    base.update(kwargs)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def test_no_fallback_configured_leaves_the_stack_as_it_was() -> None:
    client = build(_settings())
    assert not isinstance(getattr(client, "inner", None), Failover)


def test_a_configured_fallback_wraps_the_primary_and_carries_the_remap() -> None:
    client = build(_settings(model_fallback_backend="claude_cli"))
    chain = getattr(client, "inner", None)
    assert isinstance(chain, Failover)
    assert chain.remap == {"free/triage:free": "haiku", "free/extract:free": "sonnet"}
    assert chain.default_model == "sonnet"


def test_a_fallback_that_cannot_be_built_does_not_take_the_run_down(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 5, in the place it is easiest to forget. The primary is what the pipeline
    runs on; a secondary that cannot be constructed leaves it exactly as good as it was
    before the chain existed."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = build(_settings(model_fallback_backend="anthropic", model_fallback_api_key=""))
    assert not isinstance(getattr(client, "inner", None), Failover)
    assert "unavailable" in capsys.readouterr().err


def test_the_primarys_key_is_never_handed_to_the_secondary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`MODEL_API_KEY` belongs to the primary, and the secondary is a different provider
    by construction. Inheriting it buys a 401 on every call of the exact outage the chain
    exists to cover — and it reads as the fallback being broken rather than unconfigured.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    build(_settings(model_backend="openai_compatible", model_fallback_backend="anthropic"))
    # Built with `model_api_key="k"` set and no Anthropic key anywhere: the secondary must
    # refuse to be constructed rather than quietly reuse OpenRouter's credentials.
    settings = _settings(model_fallback_backend="anthropic")
    assert settings.model_api_key == "k"
    assert not isinstance(getattr(build(settings), "inner", None), Failover)


def test_the_run_says_how_much_the_fallback_carried(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Here the secondary is the owner's Claude subscription, so "the free tier has been
    dead for a week" and "everything is fine" look identical without this line."""
    from backglass.__main__ import _print_report
    from backglass.sync import SyncReport

    _print_report(
        SyncReport(fallback_calls=31, fallback_reason="RateLimited: free-models-per-day"),
        dry_run=False,
    )
    out = capsys.readouterr().out
    assert "fallback answered 31 call(s) the primary refused" in out
    assert "free-models-per-day" in out

    _print_report(SyncReport(), dry_run=False)
    assert "fallback" not in capsys.readouterr().out, "silent when nothing crossed over"


# ── finding the chain wherever it ended up ────────────────────────────────


def test_the_chain_is_found_through_every_nesting_build_can_produce() -> None:
    """`build` nests differently depending on configuration, and a reader that knew only
    about `.inner` would report zero forever under `APPLE_TRIAGE=1` while looking correct
    everywhere else. An honest-looking zero is the worst answer available here: it says
    "the free tier is fine" on exactly the runs where it is not."""
    from backglass.extract.client import AppleShortcutBackend, TriageRouter, fallback_stats

    chain = a_chain(Recorder(raises=RateLimited("HTTP 429: free-models-per-day")), Recorder())
    call(chain)

    bare = AuthCircuit(chain)
    routed = AuthCircuit(
        TriageRouter(primary=chain, triage=AppleShortcutBackend(shortcut="x"))
    )
    for stack, label in ((chain, "unwrapped"), (bare, "AuthCircuit"), (routed, "TriageRouter")):
        used, reason = fallback_stats(stack)
        assert used == 1, f"not found through {label}"
        assert "free-models-per-day" in reason


def test_no_chain_in_the_stack_reports_zero_not_an_error() -> None:
    from backglass.extract.client import fallback_stats

    assert fallback_stats(AuthCircuit(Recorder())) == (0, "")
