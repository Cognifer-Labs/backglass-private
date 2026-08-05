"""Apple Intelligence triage screen. Phase 7.

Fake runner throughout — no test invokes `shortcuts`. The invariant under test:
Apple screens, Claude extracts, and every Apple-side failure degrades to the
primary backend rather than to lost mail.
"""

from __future__ import annotations

from typing import Any

import pytest

from backglass.config import Settings
from backglass.extract import client as client_mod
from backglass.extract.client import (
    AppleShortcutBackend,
    ModelError,
    ModelResult,
    TriageRouter,
)

TRIAGE_SCHEMA = {"properties": {"keep": {}, "reason": {}}}
EXTRACT_SCHEMA = {"properties": {"commitments": {}}}


def backend(output: str) -> AppleShortcutBackend:
    return AppleShortcutBackend(runner=lambda argv, stdin: output)


def call(b: Any, schema: dict[str, Any]) -> ModelResult:
    return b.complete(system="s", user="u", schema=schema, model="pcc", budget_usd=0.1)


class TestAppleShortcutBackend:
    def test_keep_and_drop_lines_parse(self) -> None:
        kept = call(backend("KEEP: names a deadline"), TRIAGE_SCHEMA)
        assert kept.data == {"keep": True, "reason": "names a deadline"}
        assert kept.cost_usd == 0.0
        dropped = call(backend("drop: newsletter"), TRIAGE_SCHEMA)
        assert dropped.data["keep"] is False

    def test_unparseable_output_keeps_by_default(self) -> None:
        result = call(backend("The message seems interesting overall."), TRIAGE_SCHEMA)
        assert result.data["keep"] is True
        assert "unparseable" in result.data["reason"]

    def test_refuses_extraction_schemas(self) -> None:
        with pytest.raises(ModelError):
            call(backend("KEEP: x"), EXTRACT_SCHEMA)

    def test_prompt_carries_the_answer_contract(self) -> None:
        seen: dict[str, str] = {}

        def spy(argv: list[str], stdin: str) -> str:
            seen["argv"] = " ".join(argv)
            seen["stdin"] = stdin
            return "KEEP: ok"

        call(AppleShortcutBackend(runner=spy), TRIAGE_SCHEMA)
        assert "shortcuts run" in seen["argv"]
        assert "KEEP: <short reason> or DROP:" in seen["stdin"]


class FakePrimary:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, **kwargs: Any) -> ModelResult:
        self.calls.append("keep" if "keep" in kwargs["schema"]["properties"] else "extract")
        return ModelResult(data={"keep": True, "reason": "primary"}, cost_usd=0.003)


class TestTriageRouter:
    def test_triage_goes_to_apple_extraction_to_primary(self) -> None:
        primary = FakePrimary()
        router = TriageRouter(primary=primary, triage=backend("DROP: bulk"))
        triaged = call(router, TRIAGE_SCHEMA)
        assert triaged.data["keep"] is False and triaged.cost_usd == 0.0
        call(router, EXTRACT_SCHEMA)
        assert primary.calls == ["extract"]

    def test_apple_failure_falls_back_to_primary(self) -> None:
        def broken(argv: list[str], stdin: str) -> str:
            raise ModelError("shortcut 'Backglass Triage' failed")

        primary = FakePrimary()
        router = TriageRouter(
            primary=primary, triage=AppleShortcutBackend(runner=broken)
        )
        result = call(router, TRIAGE_SCHEMA)
        assert result.data["reason"] == "primary"
        assert primary.calls == ["keep"]

    def test_build_wraps_only_when_enabled(self) -> None:
        # Pinned explicitly, matching tests/conftest.py's `settings` fixture: this is
        # about apple_triage wrapping, not backend selection, so it must not inherit
        # whatever MODEL_BACKEND a real .env (or its absence) happens to leave in
        # effect — `Settings` reads .env even when constructed with explicit kwargs.
        base = Settings(owner_emails=["x@y.z"], model_backend="claude_cli")
        wrapped = Settings(owner_emails=["x@y.z"], model_backend="claude_cli",
                           apple_triage=True, apple_triage_shortcut="My Screen")
        # `.inner`: build() now returns every backend inside an AuthCircuit, so that one
        # rejected credential stops a run instead of being re-asked once per item (see
        # tests/test_model_auth.py). The routing question this test asks is unchanged —
        # it is just one wrapper further in.
        assert not isinstance(client_mod.build(base).inner, TriageRouter)
        router = client_mod.build(wrapped).inner
        assert isinstance(router, TriageRouter)
        assert router.triage.shortcut == "My Screen"
