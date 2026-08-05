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
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
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


class RateLimited(ModelError):
    """The backend refused because a usage window is exhausted, not because it failed.

    The distinction is the whole point. A malformed response is the model getting the
    item wrong, and extract-commitments.md §Failure handling says park it after two
    attempts. A rate limit says nothing about the item at all: retrying it now meets the
    same wall, and parking it throws away work that would have succeeded an hour later.
    So this never consumes an attempt, never parks, and stops the wave — the items stay
    PENDING and the next scheduled sync picks them up (CLAUDE.md rule 5, in the direction
    that does not lose data).

    Server overload (529) rides the same path for the same reason: it clears by itself.
    """


class ModelAuthError(ModelError):
    """The backend rejected our credentials.

    Split out from `ModelError` because the two failures need opposite handling and,
    until 2026-08-05, were indistinguishable in `run.errors_json`. A malformed response
    is about *this item* — restating the schema and asking again is the documented cure
    (extract-commitments.md §Failure handling). A rejected credential is about the
    *backend*: it is identical for every item, no retry can fix it, and every extra
    attempt is another subprocess spent proving the same thing.

    The distinction is load-bearing for triage of the failure itself. A run whose model
    returned junk means the prompt or the model is wrong. A run whose model could not
    authenticate means nothing was read at all — the items are parked, not judged — and
    the owner has to go re-authenticate something. One is a quality problem, the other
    is an outage, and reading them off the same sentence cost six hours on 2026-08-05.
    """


#: Substrings that identify an authentication failure in a backend's own words. The CLI
#: backend hands back the model's error as prose, so this is pattern matching and is
#: kept deliberately narrow: every phrase here names a credential being rejected, not a
#: request being refused for some other reason. A miss degrades to the old behaviour
#: (retry, then park); a false positive would stop a run early, so nothing vague like
#: "denied" or "forbidden" belongs in this list.
#:
#: Consulted *before* `_looks_rate_limited`, and the order is a decision rather than an
#: accident. The two vocabularies look disjoint — nothing here names a window and nothing
#: there names a credential — but if a sentence ever satisfies both, auth has to win. The
#: two mistakes are not symmetrical: a wrong rate-limit verdict stops the wave and spends
#: no attempt, which is the shape that stalls a queue indefinitely (see `_LimitClaim`),
#: while a wrong auth verdict degrades to an ordinary park after this item's attempts.
#: Ties resolve toward the bounded failure.
_AUTH_SIGNATURES = (
    "oauth session expired",
    "oauth token has expired",
    "failed to authenticate",
    "authentication_failed",
    "authentication failed",
    "invalid api key",
    "invalid_api_key",
    "invalid bearer token",
    "please run /login",
    "oauth_org_not_allowed",
    "api key authentication is disabled",
    "could not be refreshed",
)


def _is_auth_failure(text: str) -> bool:
    lowered = text.lower()
    return any(signature in lowered for signature in _AUTH_SIGNATURES)


@dataclass(frozen=True)
class ModelResult:
    data: dict[str, Any]
    cost_usd: float


class ModelClient(Protocol):
    #: Is `ModelResult.cost_usd` money, or a price nobody is charged?
    #:
    #: A subscription backend still reports a number — the CLI's `total_cost_usd` is what
    #: the same call would have cost on the API — and reporting it is useful. Enforcing a
    #: cap against it is not: it stops work over a bill that will never arrive. Backends
    #: that bill per call leave this False and the cap in sync.py stays hard.
    spend_is_imputed: bool

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
    #: Subscription auth. `total_cost_usd` is the API-equivalent price of the call, and
    #: measurably not even proportional to the payload — a trivial prompt reported ~4c,
    #: dominated by the CLI's own session cache_creation tokens. Recorded, never enforced.
    spend_is_imputed: bool = True

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
            except ModelAuthError:
                # Not a schema failure, so `_restate` has nothing to fix. The second
                # attempt would spawn a second CLI process against the same rejected
                # session — on 2026-08-05 that doubled a six-hour outage into 44 wasted
                # subprocesses. Surface it immediately and let the caller stop.
                #
                # A sibling of RateLimited below, not a subclass: both skip the retry,
                # but for opposite reasons and to opposite ends. A shut window leaves the
                # item pending for the next sync; a rejected credential parks it and says
                # so. Neither may be read as the other.
                raise
            except RateLimited as exc:
                # Not an attempt. The window is shut; a second call inside the same second
                # would only confirm it, and the caller has to stop rather than park.
                exc.cost_usd = spent + getattr(exc, "cost_usd", 0.0)  # type: ignore[attr-defined]
                raise
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
        # `--max-budget-usd` is deliberately absent. It is a hard ceiling against the same
        # imputed price `spend_is_imputed` describes, so on subscription auth it aborts
        # real calls over money nobody is charged — and it is not proportional to the
        # payload, because the CLI's own session cache_creation tokens dominate it. What
        # actually bounds a call here is `timeout_seconds` and, upstream,
        # `per_item_char_ceiling`. Restore this flag the moment the CLI bills per call.
        del budget_usd
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
            # The CLI can die before it writes an envelope, and the reason is then only on
            # stderr — a shut usage window read as a plain failure would park the item,
            # and a rejected session read as one would retry into the same refusal.
            #
            # What this branch is allowed to match is the whole point, because the two
            # mistakes are not symmetrical. Reading a real limit as a failure parks one
            # item after its two attempts and lets the rest of the pass continue: bounded,
            # and visible in the run's errors. Reading a crash as a limit breaks the whole
            # pass and consumes no attempt, so an item that fails deterministically stalls
            # the queue behind it on every later sync — while the panel promises a retry.
            # Only phrases a limit actually uses may reach here; see the marker list.
            # Auth is asked first — see `_AUTH_SIGNATURES` on why ties go that way.
            detail = completed.stderr[:300]
            if _is_auth_failure(detail):
                raise ModelAuthError(f"authentication failed: {detail.strip()}")
            if _looks_rate_limited(completed.stderr):
                raise RateLimited(
                    f"model rate limit (exit {completed.returncode}): "
                    f"{completed.stderr.strip()[:200]}"
                )
            raise ModelError(f"empty response (exit {completed.returncode}): {detail}")
        try:
            envelope = json.loads(completed.stdout)
        except ValueError as exc:
            raise ModelError(f"response was not JSON: {completed.stdout[:300]}") from exc

        cost = float(envelope.get("total_cost_usd") or 0.0)
        if envelope.get("is_error"):
            detail = str(envelope.get("result"))[:300]
            if _is_auth_failure(detail):
                # `is_error` is how the CLI reports "your subscription session was
                # rejected" as well as "the model misbehaved" and "the window is shut".
                # Only the prose tells them apart, so it is read here rather than guessed
                # at upstream — auth first, per `_AUTH_SIGNATURES`.
                raise ModelAuthError(f"authentication failed: {detail}")
            error: ModelError
            if _rate_limited_envelope(envelope):
                error = RateLimited(f"model rate limit: {detail}")
            else:
                error = ModelError(f"model reported an error: {detail}")
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


#: Subtypes the CLI's `-p --output-format json` envelope can carry, read out of the
#: shipped binary (2.1.221, verified 2026-08-04): success, error_during_execution,
#: error_max_turns, error_max_budget_usd, error_max_structured_output_retries. None of
#: them names a rate limit, so an exhausted window can only arrive as a generic error
#: whose *text* carries the reason. These two subtypes are the model failing to produce
#: the requested shape, which is exactly the retry-then-park case, so they are never read
#: as transient however the text reads — a schema failure that happens to quote the words
#: "rate limit" back at us must still park.
_SCHEMA_FAILURE_SUBTYPES = frozenset({"error_max_turns", "error_max_structured_output_retries"})

#: The vocabulary the CLI uses for a limit that clears on its own. Every phrase is present
#: in the 2.1.221 binary's own limit and API-error strings ("usage limit reached", "rate
#: limited", "rate_limit_error", "session limit", "weekly limit", "Server is temporarily
#: limiting requests (not your usage limit)", "Repeated 529 Overloaded errors"). Matched
#: as substrings rather than pinned to one sentence, because the sentence is UI copy and
#: changes between releases while the noun does not.
#:
#: Phrases only, never a bare status number. `429` and `529` would match anywhere in
#: arbitrary text — including the column offsets of a minified Node stack trace
#: (`cli.js:1:429517`) — which turns a crashed subprocess into a "rate limit", breaks the
#: whole pass, and consumes no attempt, so the crashing item stalls the queue behind it on
#: every later sync while the panel promises a retry. `rate_limit_error` is Anthropic's own
#: error type and `overloaded` is 529's own word, so neither number buys anything the
#: phrases miss.
#:
#: NOT confirmed against a live rate-limited envelope — no such envelope has been captured
#: from this machine. The cost of a false negative is the old behaviour (park after two
#: attempts); the cost of a false positive is one stopped wave whose items retry next
#: sync. Both are recoverable, and we only ever consult this on an error the backend
#: already declared.
_TRANSIENT_LIMIT_MARKERS = (
    "usage limit",
    "rate limit",
    "rate_limit_error",
    "rate limited",
    "session limit",
    "weekly limit",
    "temporarily limiting requests",
    "overloaded",
)

#: The wrapper the CLI actually renders its limits through, which the marker list above
#: read too narrowly. Its label table is
#: `{five_hour: "session limit", seven_day: "weekly limit", seven_day_opus: "Opus limit",
#: seven_day_sonnet: "Sonnet limit", seven_day_overage_included: "Fable 5 limit",
#: overage: "usage credit limit"}`, rendered as `You've hit your ${label}` — so four of the
#: six carry no phrase from the list, including the two per-model weekly limits, which are
#: the ones a pipeline that pins `model_triage` and `model_extract` is most likely to meet.
#: ("usage credit limit" does not contain "usage limit" either.)
#:
#: Matched on the wrapper rather than the six labels because the labels are the part that
#: changes: a seventh model gets a seventh label, and a marker list that has to be revised
#: every time a model ships is a marker list that is silently wrong between releases. The
#: leading `You've` is deliberately not matched — the apostrophe is typographic in some
#: renderings — and the gap is bounded so this stays a phrase and not a pair of words that
#: could meet across a paragraph.
#:
#: Still phrases only. This is a regex over words, never a status number; the reasoning in
#: the marker list above about `cli.js:1:429517` is what that rule is protecting.
_TRANSIENT_LIMIT_PATTERN = re.compile(r"hit your .{0,40}?limit")


def _looks_rate_limited(text: str | None) -> bool:
    lowered = (text or "").lower()
    if any(marker in lowered for marker in _TRANSIENT_LIMIT_MARKERS):
        return True
    return _TRANSIENT_LIMIT_PATTERN.search(lowered) is not None


def _rate_limited_envelope(envelope: dict[str, Any]) -> bool:
    if str(envelope.get("subtype") or "") in _SCHEMA_FAILURE_SUBTYPES:
        return False
    # `errors` is a list the error_during_execution variant carries alongside `result`;
    # the reason lands in whichever of the three the CLI chose to fill.
    return _looks_rate_limited(
        f"{envelope.get('result')} {envelope.get('error')} {envelope.get('errors')}"
    )


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
    #: Billed per call. The monthly cap is real money and stays hard.
    spend_is_imputed: bool = False

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
            if exc.code in (401, 403):
                raise ModelAuthError(
                    f"authentication failed: deepinfra returned HTTP {exc.code}"
                ) from exc
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
    #: Billed per call, from real usage counts. The monthly cap is real money.
    spend_is_imputed: bool = False

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
            except ModelAuthError:
                raise  # a rejected key is not a schema failure; see ClaudeCLIBackend
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
            # anthropic.AuthenticationError/PermissionDeniedError carry status_code;
            # the name check keeps this working against the injected test doubles,
            # which are not the real SDK classes.
            if getattr(exc, "status_code", None) in (401, 403) or _is_auth_failure(
                f"{type(exc).__name__} {exc}"
            ):
                raise ModelAuthError(
                    f"authentication failed: {type(exc).__name__}: {exc}"
                ) from exc
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
        return AuthCircuit(
            TriageRouter(
                primary=primary,
                triage=AppleShortcutBackend(shortcut=settings.apple_triage_shortcut),
            )
        )
    # Outermost, so every caller — sync, batch, brief, the roadmap interview — gets the
    # same one-strike behaviour without knowing the wrapper exists.
    return AuthCircuit(primary)


#: Which class `build` would choose, keyed by the setting. Exists so the question "does
#: this configuration cost money" can be answered without constructing a backend — every
#: caller that asks (costs.py, the dashboard, `doctor`) would otherwise need an API key
#: to find out that it does not need one.
_BACKEND_CLASSES: dict[str, type] = {
    "deepinfra": DeepInfraBackend,
    "anthropic": AnthropicAPIBackend,
    "claude_cli": ClaudeCLIBackend,
}


def spend_is_imputed(settings: Settings) -> bool:
    """Is the configured backend's reported cost a price rather than a charge?

    Read off the same classes `build` returns, so a backend cannot report one answer
    to the cap and another to the page describing it. An unknown name resolves the way
    `build` resolves one — to the CLI.
    """
    chosen = _BACKEND_CLASSES.get(settings.model_backend, ClaudeCLIBackend)
    return bool(getattr(chosen, "spend_is_imputed", False))


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
    #: Apple's model on the owner's own machine. Nothing is metered and nothing is
    #: imputed either — this backend reports 0.0 — but the honest answer to "is that a
    #: bill" is no.
    spend_is_imputed: bool = True

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

    @property
    def spend_is_imputed(self) -> bool:
        """The primary's answer. Triage here is free, so whether the cap means anything
        is entirely a question about where extraction goes."""
        return getattr(self.primary, "spend_is_imputed", False)

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


# ─────────────────────────────────────────────────── authentication circuit


@dataclass
class AuthCircuit:
    """Stops calling a backend that has already said our credentials are no good.

    An authentication failure is a property of the process, not of the item being read,
    so the first one has already told us the answer for every item still queued. Without
    this, 2026-08-05 run 118 sent eleven items at a session the CLI had already rejected
    ten times, and — before the retry fix above — did it twice each.

    What this deliberately does *not* do is swallow the failure. Every queued item still
    raises, so every one of them still lands in `run.errors_json` and stays untriaged and
    visible. What changes is that only the first attempt costs a subprocess; the rest are
    answered from memory. Parked loudly and immediately beats parked slowly and silently.

    Scope is one process, which is one run — `build()` is called per invocation, so the
    next scheduled sync starts with the circuit closed and re-tests the session. A run
    that trips at 06:00 does not suppress the 06:30 run's chance to recover.

    Only `ModelAuthError` trips it, and `RateLimited` deliberately passes straight
    through unwrapped. They are siblings, not parent and child, so this is true by
    construction rather than by care — but it is load-bearing enough to be tested: a shut
    usage window must still reach `_LimitClaim` as itself, leave its items PENDING for the
    next sync, and never be re-told as an outage the owner has to go fix by hand.
    """

    inner: Any
    #: Mirrors the wrapped backend rather than deciding anything, so the spend cap sees
    #: through the wrapper. A plain field, not a property: `ModelClient` declares this a
    #: settable attribute, and a read-only property here would not satisfy it.
    spend_is_imputed: bool = False
    _tripped: ModelAuthError | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.spend_is_imputed = bool(getattr(self.inner, "spend_is_imputed", False))

    def complete(
        self, *, system: str, user: str, schema: dict[str, Any], model: str, budget_usd: float
    ) -> ModelResult:
        # Read under the lock: triage and extraction fan out to `max_concurrency`
        # threads, so this is genuinely concurrent.
        with self._lock:
            tripped = self._tripped
        if tripped is not None:
            raise ModelAuthError(
                "authentication failed earlier in this run; no further model calls "
                f"attempted ({tripped})"
            )
        try:
            result: ModelResult = self.inner.complete(
                system=system, user=user, schema=schema, model=model, budget_usd=budget_usd
            )
            return result
        except ModelAuthError as exc:
            with self._lock:
                if self._tripped is None:
                    self._tripped = exc
            raise
