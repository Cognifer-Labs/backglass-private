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


# ─────────────────────────────────────────────────────────────── helpers


def build(settings: Settings) -> ModelClient:
    primary: ModelClient
    if settings.model_backend == "deepinfra":
        if not settings.model_api_key:
            raise ModelError("MODEL_BACKEND=deepinfra but MODEL_API_KEY is empty")
        primary = DeepInfraBackend(
            api_key=settings.model_api_key, base_url=settings.deepinfra_base_url
        )
    else:
        primary = ClaudeCLIBackend()
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
