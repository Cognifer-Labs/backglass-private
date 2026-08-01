"""The model layer, behind one interface with two backends.

docs/10 §Model layer specifies the Anthropic SDK with structured output forced through
tool use. This build runs on the Claude Code CLI instead, because the owner's model
access is a subscription rather than an API key. The guarantee survives the substitution:
`--json-schema` is implemented as a forced tool call, and the CLI returns the validated
object in a separate `structured_output` field. What changes is the transport, not the
enforcement. See tasks/todo.md §Deviations #1.

`DeepInfraBackend` is the product path — open-source models over an OpenAI-compatible
API, where tool-use schema enforcement is native and the shape docs/10 describes is
restored exactly.

Both backends are constructed from config. Nothing above this module knows which is in
use.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from backglass.config import Settings

#: Replaces the CLI's default agent system prompt entirely. The default is written for an
#: interactive coding agent and is worth about a thousand input tokens on every call.
SYSTEM = (
    "You extract structured records from a single message. Follow the instructions in the "
    "message exactly. Return only the structured output. Do not explain, do not comment, "
    "do not use any tools other than the structured output."
)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ModelError(RuntimeError):
    """The model call failed, or returned something that is not the requested shape."""


@dataclass(frozen=True)
class ModelResult:
    data: dict[str, Any]
    cost_usd: float


class ModelClient(Protocol):
    def complete(
        self, *, system: str, user: str, schema: dict[str, Any], model: str, budget_usd: float
    ) -> ModelResult: ...


# ────────────────────────────────────────────────────────────── Claude CLI


#: Measured 2026-07-30: $0.0031 per triage call with these flags, $0.307 without.
#: The difference is CLAUDE.md, the MCP tool schemas and the skill catalogue being loaded
#: into every single call. --safe-mode drops all of it and leaves subscription auth
#: working. Do not remove any of these. See tasks/lessons.md.
CLI_ISOLATION_FLAGS = (
    "--safe-mode",
    "--tools",
    "",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--disable-slash-commands",
    "--no-session-persistence",
)


@dataclass
class ClaudeCLIBackend:
    executable: str = "claude"
    timeout_seconds: int = 240
    #: extract-commitments.md §Failure handling: "Malformed JSON: retry once with the
    #: schema restated. On second failure, park the item."
    attempts: int = 2

    def complete(
        self, *, system: str, user: str, schema: dict[str, Any], model: str, budget_usd: float
    ) -> ModelResult:
        spent = 0.0
        last: Exception | None = None
        for attempt in range(self.attempts):
            prompt = user if attempt == 0 else _restate(user, schema)
            try:
                data, cost = self._run(system, prompt, schema, model, budget_usd)
                return ModelResult(data=data, cost_usd=spent + cost)
            except ModelError as exc:
                last = exc
                spent += getattr(exc, "cost_usd", 0.0)
        raise ModelError(
            f"model returned an unusable response after {self.attempts} attempts: {last}"
        )

    def _run(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        model: str,
        budget_usd: float,
    ) -> tuple[dict[str, Any], float]:
        command = [
            self.executable,
            "-p",
            "--model",
            model,
            *CLI_ISOLATION_FLAGS,
            "--output-format",
            "json",
            "--system-prompt",
            system,
            "--json-schema",
            json.dumps(schema),
            "--max-budget-usd",
            str(budget_usd),
        ]
        try:
            completed = subprocess.run(
                command,
                input=user,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ModelError(f"{self.executable} is not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise ModelError(f"model call exceeded {self.timeout_seconds}s") from exc

        if not completed.stdout.strip():
            raise ModelError(
                f"empty response (exit {completed.returncode}): {completed.stderr[:300]}"
            )
        try:
            envelope = json.loads(completed.stdout)
        except ValueError as exc:
            raise ModelError(f"response was not JSON: {completed.stdout[:300]}") from exc

        cost = float(envelope.get("total_cost_usd") or 0.0)
        if envelope.get("is_error"):
            error = ModelError(f"model reported an error: {str(envelope.get('result'))[:300]}")
            error.cost_usd = cost  # type: ignore[attr-defined]
            raise error

        structured = envelope.get("structured_output")
        if isinstance(structured, dict):
            return structured, cost

        # Fallback. The CLI populates structured_output when the schema call succeeds; if
        # it did not, the text may still hold the object. Parsed rather than trusted, and
        # a failure here is what triggers the retry.
        parsed = _parse_loose(str(envelope.get("result", "")))
        if parsed is None:
            error = ModelError("no structured_output and the text was not parseable JSON")
            error.cost_usd = cost  # type: ignore[attr-defined]
            raise error
        return parsed, cost


# ────────────────────────────────────────────────────────────── DeepInfra


@dataclass
class DeepInfraBackend:
    """The product path. OpenAI-compatible chat completions with a forced tool call.

    This is the shape docs/10 §Model layer actually specifies — the model calls a single
    tool whose input schema is the extraction schema, so malformed output is rejected at
    the API layer rather than salvaged from prose.
    """

    api_key: str
    base_url: str = "https://api.deepinfra.com/v1/openai"
    timeout_seconds: int = 120
    tool_name: str = "emit"

    def complete(
        self, *, system: str, user: str, schema: dict[str, Any], model: str, budget_usd: float
    ) -> ModelResult:
        del budget_usd  # per-call cost is bounded by the monthly cap in sync.py
        body = json.dumps(
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": self.tool_name,
                            "description": "Return the extracted records.",
                            "parameters": schema,
                        },
                    }
                ],
                "tool_choice": {"type": "function", "function": {"name": self.tool_name}},
            }
        ).encode()

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                envelope = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise ModelError(f"deepinfra returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise ModelError(f"deepinfra call failed: {type(exc).__name__}") from exc

        try:
            call = envelope["choices"][0]["message"]["tool_calls"][0]
            data = json.loads(call["function"]["arguments"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelError("deepinfra response contained no usable tool call") from exc
        if not isinstance(data, dict):
            raise ModelError("tool arguments were not an object")
        return ModelResult(
            data=data, cost_usd=float(envelope.get("usage", {}).get("estimated_cost", 0.0))
        )


# ────────────────────────────────────────────────────────────── Anthropic API


def build_request(
    *,
    system: str,
    user: str,
    schema: dict[str, Any],
    model_id: str,
    max_tokens: int,
    tool_name: str = "emit",
) -> dict[str, Any]:
    """One request shape for both the live API and the Batches API (backglass/batch.py).

    Forced tool use with the extraction schema as the tool's input schema — the same
    enforcement docs/10 §Model layer specifies. The system block carries a
    cache_control marker so the static prefix (SYSTEM + the prompt's instruction half,
    see prompts.Prompt.split) is billed at the ~0.1x cache-read rate after the first
    call. Honest economics: the cacheable minimum is model-dependent (sonnet-4-6
    ~1024 tokens, haiku-4-5 ~4096), so haiku triage likely never caches — the marker
    is harmless there; the real win is sonnet extraction. Verify with
    usage.cache_read_input_tokens > 0 on a second call.
    """
    return {
        "model": model_id,
        "max_tokens": max_tokens,
        "system": [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ],
        "messages": [{"role": "user", "content": user}],
        "tools": [
            {
                "name": tool_name,
                "description": "Return the extracted records.",
                "input_schema": schema,
            }
        ],
        "tool_choice": {"type": "tool", "name": tool_name},
    }


@dataclass(kw_only=True)
class AnthropicAPIBackend:
    """The Anthropic API directly, with prompt caching.

    Same schema enforcement as DeepInfraBackend — a single forced tool whose input
    schema is the extraction schema — plus cache_control on the static prefix. Cost is
    computed from real usage token counts against extract/pricing.py, never estimated.

    Budget semantics: the CLI backend's --max-budget-usd is a hard server-side
    ceiling; here the guard is a pre-flight worst-case estimate (chars/3.5 ≈ tokens),
    which parks pathological items before any spend. Softer than the CLI's, but
    per_item_char_ceiling bounds input upstream and the monthly SpendCap in sync.py
    remains the hard backstop.
    """

    api_key: str
    timeout_seconds: float = 120.0
    #: extract-commitments.md §Failure handling, same as ClaudeCLIBackend.
    attempts: int = 2
    max_tokens_triage: int = 1024
    max_tokens_extract: int = 4096
    tool_name: str = "emit"
    client: Any | None = None  # injected in tests, like AppleShortcutBackend.runner

    def _client(self) -> Any:
        if self.client is None:
            import anthropic

            # SDK retries 408/429/5xx (incl. 529 overloaded) with backoff and honors
            # retry-after; no hand-rolled loop.
            self.client = anthropic.Anthropic(
                api_key=self.api_key, max_retries=4, timeout=self.timeout_seconds
            )
        return self.client

    def complete(
        self, *, system: str, user: str, schema: dict[str, Any], model: str, budget_usd: float
    ) -> ModelResult:
        from backglass.extract import pricing

        model_id = pricing.resolve(model)
        # Triage schemas (single or batch) get the small output budget; extraction the
        # large one. Discriminated on the schema, like every test double and router.
        props = schema.get("properties", {})
        max_tokens = (
            self.max_tokens_triage
            if "keep" in props or "items" in props
            else self.max_tokens_extract
        )

        price = pricing.PRICES.get(model_id)
        if price is not None:
            worst_case = (
                (len(system) + len(user)) / 3.5 * price.input
                + max_tokens * price.output
            ) / 1_000_000
            if worst_case > budget_usd:
                raise ModelError(
                    f"estimated worst case ${worst_case:.3f} exceeds the per-call "
                    f"budget ${budget_usd}; parked"
                )

        spent = 0.0
        last: Exception | None = None
        for attempt in range(self.attempts):
            prompt = user if attempt == 0 else _restate(user, schema)
            try:
                data, cost = self._run(system, prompt, schema, model_id, max_tokens)
                return ModelResult(data=data, cost_usd=spent + cost)
            except ModelError as exc:
                last = exc
                spent += getattr(exc, "cost_usd", 0.0)
        raise ModelError(
            f"model returned an unusable response after {self.attempts} attempts: {last}"
        )

    def _run(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        model_id: str,
        max_tokens: int,
    ) -> tuple[dict[str, Any], float]:
        from backglass.extract import pricing

        params = build_request(
            system=system,
            user=user,
            schema=schema,
            model_id=model_id,
            max_tokens=max_tokens,
            tool_name=self.tool_name,
        )
        try:
            response = self._client().messages.create(**params)
        except ModelError:
            raise
        except Exception as exc:  # noqa: BLE001 - anthropic.APIError et al → degrade path
            raise ModelError(f"anthropic call failed: {type(exc).__name__}: {exc}") from exc

        cost = pricing.cost_usd(model_id, getattr(response, "usage", None))
        block = next(
            (b for b in response.content if getattr(b, "type", "") == "tool_use"), None
        )
        data = getattr(block, "input", None)
        if not isinstance(data, dict):
            error = ModelError("response contained no usable tool call")
            error.cost_usd = cost  # type: ignore[attr-defined]
            raise error
        return data, cost


# ─────────────────────────────────────────────────────────────── helpers


def _find_claude() -> str:
    """Locate the claude CLI without relying on a shell PATH.

    A Finder-launched app and a launchd job both get a minimal PATH, and the CLI
    cannot be bundled (subscription auth, self-updating). Precedence: explicit
    override, PATH, then the known install locations.
    """
    import os
    import shutil

    if explicit := os.environ.get("CLAUDE_BIN"):
        return explicit
    if found := shutil.which("claude"):
        return found
    home = Path(os.path.expanduser("~"))
    for candidate in (
        home / ".claude" / "local" / "claude",
        home / ".local" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ):
        if candidate.exists():
            return str(candidate)
    raise ModelError(
        "claude CLI not found on PATH or in known locations; set CLAUDE_BIN"
    )


def anthropic_api_key(settings: Settings) -> str:
    import os

    return settings.model_api_key or os.environ.get("ANTHROPIC_API_KEY", "")


def build(settings: Settings) -> ModelClient:
    primary: ModelClient
    if settings.model_backend == "deepinfra":
        if not settings.model_api_key:
            raise ModelError("MODEL_BACKEND=deepinfra but MODEL_API_KEY is empty")
        primary = DeepInfraBackend(
            api_key=settings.model_api_key, base_url=settings.deepinfra_base_url
        )
    elif settings.model_backend == "anthropic":
        key = anthropic_api_key(settings)
        if not key:
            raise ModelError(
                "MODEL_BACKEND=anthropic but MODEL_API_KEY and ANTHROPIC_API_KEY are empty"
            )
        primary = AnthropicAPIBackend(api_key=key)
    else:
        primary = ClaudeCLIBackend(executable=_find_claude())
    if settings.apple_triage:
        return TriageRouter(
            primary=primary,
            triage=AppleShortcutBackend(shortcut=settings.apple_triage_shortcut),
        )
    return primary


def _restate(user: str, schema: dict[str, Any]) -> str:
    return (
        f"{user}\n\nYour previous response did not match the required schema. "
        f"Return exactly this shape and nothing else:\n{json.dumps(schema)}"
    )


def _parse_loose(text: str) -> dict[str, Any] | None:
    text = text.strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ─────────────────────────────────────────────── Apple Intelligence (triage only)


#: The one third-party door into Apple's models — including Private Cloud Compute —
#: is Shortcuts' "Use Model" action. `shortcuts run` reaches it from a pipeline. It
#: cannot enforce a JSON schema, which is why this backend is offered for triage
#: only: a binary keep/drop parsed from a fixed token survives free-form output,
#: and triage.md already rules "when genuinely uncertain, return keep=true", which
#: is exactly the safe default when parsing fails.
APPLE_TRIAGE_PROMPT_SUFFIX = (
    "\n\nAnswer with exactly one line: KEEP: <short reason> or DROP: <short reason>."
)


@dataclass(kw_only=True)
class AppleShortcutBackend:
    """Triage through a user-created Shortcut running Apple's model.

    The shortcut is three actions, created once by the owner in Shortcuts.app:
      1. Receive Text input
      2. Use Model — model: Private Cloud Compute (or On-Device), prompt: Shortcut Input
      3. Stop and output — Model Response
    Named per `apple_triage_shortcut` ("Backglass Triage" by default). Content sent
    here stays inside Apple's privacy boundary; nothing reaches Anthropic until an
    item has survived this screen.
    """

    shortcut: str = "Backglass Triage"
    timeout_seconds: int = 120
    runner: Callable[[list[str], str], str] | None = None  # injected in tests

    def _run(self, argv: list[str], stdin: str) -> str:
        if self.runner is not None:
            return self.runner(argv, stdin)
        done = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        if done.returncode != 0:
            raise ModelError(done.stderr.strip() or f"shortcut {self.shortcut!r} failed")
        return done.stdout

    def complete(
        self, *, system: str, user: str, schema: dict[str, Any], model: str, budget_usd: float
    ) -> ModelResult:
        del model, budget_usd  # the OS picks the model; PCC is not metered
        if "keep" not in schema.get("properties", {}):
            raise ModelError(
                "AppleShortcutBackend handles triage only — extraction needs the "
                "schema-enforcing backend"
            )
        out = self._run(
            ["shortcuts", "run", self.shortcut, "-i", "-"],
            f"{system}\n\n{user}{APPLE_TRIAGE_PROMPT_SUFFIX}",
        )
        line = out.strip().splitlines()[0].strip() if out.strip() else ""
        upper = line.upper()
        if upper.startswith("DROP"):
            keep, reason = False, line.partition(":")[2].strip() or "dropped"
        elif upper.startswith("KEEP"):
            keep, reason = True, line.partition(":")[2].strip() or "kept"
        else:
            # triage.md: uncertain → keep. An unparseable screen must never lose mail.
            keep, reason = True, f"unparseable triage output kept by default: {line[:80]!r}"
        return ModelResult(data={"keep": keep, "reason": reason}, cost_usd=0.0)


@dataclass(kw_only=True)
class TriageRouter:
    """Routes triage calls to Apple's model, everything else to the primary backend.

    Routed on the schema, not a flag — the schema is generated from the pydantic
    model and cannot drift from what the caller validates against (the same
    discrimination the test doubles use). Any Apple-side failure falls back to the
    primary backend: a missing shortcut degrades to the old cost model, never to
    unclassified mail.
    """

    primary: ModelClient
    triage: AppleShortcutBackend
    fallback_noted: bool = False

    def complete(
        self, *, system: str, user: str, schema: dict[str, Any], model: str, budget_usd: float
    ) -> ModelResult:
        if "keep" in schema.get("properties", {}):
            try:
                return self.triage.complete(
                    system=system, user=user, schema=schema, model=model,
                    budget_usd=budget_usd,
                )
            except (ModelError, subprocess.TimeoutExpired) as exc:
                if not self.fallback_noted:
                    self.fallback_noted = True
                    print(
                        f"apple triage unavailable ({exc}); falling back to "
                        f"{type(self.primary).__name__}",
                        file=sys.stderr,
                    )
        return self.primary.complete(
            system=system, user=user, schema=schema, model=model, budget_usd=budget_usd
        )
