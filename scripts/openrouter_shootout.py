"""Which OpenRouter free models can actually run this pipeline? Ask them, don't guess.

The backend forces a tool call — `tool_choice: {"type": "function", ...}` in
`extract/client.py` — against the real extraction schema, which is 100+ fields deep with
nested objects and enums. A model that *advertises* `tools` in OpenRouter's catalogue has
not shown it can do that; advertising is a capability flag, and the failure this exists to
catch is the one where every sync returns an unusable response and the ledger quietly
stops growing.

So this sends each candidate the genuine `CommitmentExtraction` schema and a realistic
item, and reports what came back. Read-only against the ledger: it opens no database and
writes nothing but its own report.

    MODEL_API_KEY=sk-or-... uv run python scripts/openrouter_shootout.py
    MODEL_API_KEY=sk-or-... uv run python scripts/openrouter_shootout.py --triage

`--triage` runs the cheaper `TriageVerdict` schema instead, because triage is the
high-volume tier and a model that fails extraction may still be fine screening.

**Any OpenAI-compatible provider, not just OpenRouter.** Point it somewhere else and name
the models yourself, because only OpenRouter publishes a catalogue to enumerate:

    uv run python scripts/openrouter_shootout.py --triage \
        --base-url https://api.groq.com/openai/v1 \
        --api-key "$GROQ_API_KEY" \
        --model llama-3.3-70b-versatile --model openai/gpt-oss-120b

That is the whole acceptance test for a candidate provider. A provider that advertises
`tools` has not shown it can take *this* schema through a forced `tool_choice`, and the
failure it hides is the expensive one: every sync returning an unusable response while the
ledger quietly stops growing. Run this before pasting a key into `.env`, never after.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backglass.extract.schemas import (  # noqa: E402
    CommitmentExtraction,
    TriageVerdict,
    json_schema,
)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

#: A real-shaped item. Deliberately not a clean one — it carries a relative date, two
#: obligations in one sentence and a signature block, because those are what the ledger is
#: actually made of and a model that only handles tidy input will pass here and fail live.
FIXTURE = """From: Dr. Sheppard <asheppard@asu.edu>
Date: Mon, 17 Aug 2026 09:12:00 -0700
Subject: LSB 191 — reflection + lab safety

Dharsan,

Nice work in section. Two things before Friday: send me the one-page reflection on the
Northwind dataset, and complete the lab safety module in Canvas — you can't be in the
CHM 113 lab next week without it.

Also, I've asked Priya to share her notes with you; nothing needed from you there.

Best,
Anne Sheppard
Teaching Professor, School of Life Sciences
"""

SYSTEM = (
    "You extract typed records from a message. Call the `emit` tool exactly once with "
    "the structured result. Every field must come from the message; never invent a date."
)


def catalogue(base_url: str) -> list[dict]:
    request = urllib.request.Request(
        f"{base_url}/models", headers={"User-Agent": "backglass-shootout"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response).get("data", [])


def free_with_tools(base_url: str) -> list[dict]:
    return [
        model
        for model in catalogue(base_url)
        if model.get("id", "").endswith(":free")
        and "tools" in (model.get("supported_parameters") or [])
    ]


def attempt(model_id: str, key: str, schema: dict, timeout: int, base_url: str) -> dict:
    """One forced tool call. Returns what happened, never raises."""
    body = json.dumps(
        {
            "model": model_id,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": FIXTURE},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "emit",
                        "description": "Return the extracted records.",
                        "parameters": schema,
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "emit"}},
        }
    ).encode()
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if "openrouter" in base_url:
        # OpenRouter attributes traffic by these; harmless and honest. Sent only there,
        # because a provider that rejects unknown headers would fail for the wrong reason
        # and the report would blame the model.
        headers["HTTP-Referer"] = "https://github.com/local/backglass"
        headers["X-Title"] = "Backglass shootout"
    request = urllib.request.Request(
        f"{base_url}/chat/completions", data=body, headers=headers
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:180]
        return {
            "ok": False,
            "why": f"HTTP {exc.code}: {detail}",
            "seconds": time.time() - started,
        }
    except Exception as exc:  # noqa: BLE001 — a shootout reports, it does not crash
        return {
            "ok": False,
            "why": f"{type(exc).__name__}: {exc}",
            "seconds": time.time() - started,
        }

    seconds = time.time() - started
    choices = payload.get("choices") or []
    if not choices:
        why = f"no choices: {json.dumps(payload)[:180]}"
        return {"ok": False, "why": why, "seconds": seconds}
    message = choices[0].get("message") or {}
    calls = message.get("tool_calls") or []
    if not calls:
        text = (message.get("content") or "")[:120]
        why = f"no tool call; returned prose: {text!r}"
        return {"ok": False, "why": why, "seconds": seconds}
    raw = calls[0].get("function", {}).get("arguments", "")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"ok": False, "why": f"tool arguments were not JSON: {exc}", "seconds": seconds}
    return {
        "ok": True,
        "seconds": seconds,
        "keys": sorted(parsed)[:6],
        "commitments": len(parsed.get("commitments") or []) if isinstance(parsed, dict) else 0,
        "sample": json.dumps(parsed)[:200],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--triage", action="store_true", help="Use the triage schema")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--only", help="Substring filter on the model id")
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="Any OpenAI-compatible root, e.g. https://api.groq.com/openai/v1",
    )
    parser.add_argument("--api-key", help="Overrides MODEL_API_KEY / OPENROUTER_API_KEY")
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        dest="models",
        help="Model id to test; repeatable. Required off OpenRouter, whose catalogue "
        "is the only one this can enumerate.",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    key = (
        args.api_key
        or os.environ.get("MODEL_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY")
        or ""
    )
    if not key:
        print("pass --api-key, or set MODEL_API_KEY / OPENROUTER_API_KEY", file=sys.stderr)
        return 2

    schema = json_schema(TriageVerdict if args.triage else CommitmentExtraction)
    tier = "triage" if args.triage else "extract"

    if args.models:
        models = [{"id": model_id} for model_id in args.models]
    elif "openrouter" in base_url:
        models = free_with_tools(base_url)
        models.sort(key=lambda m: -(m.get("context_length") or 0))
    else:
        # Refusing rather than guessing: `GET /models` exists on most providers but says
        # nothing about which ids are free or tool-capable, and a report over the wrong
        # set reads exactly like a report over the right one.
        print(
            f"{base_url} is not OpenRouter, so there is no free-and-tool-capable "
            "catalogue to enumerate — name the models with --model (repeatable).",
            file=sys.stderr,
        )
        return 2
    if args.only:
        models = [m for m in models if args.only in m["id"]]

    print(f"{len(models)} model(s) at {base_url} · schema = {tier}\n")
    passed: list[tuple[str, float]] = []
    for model in models:
        model_id = model["id"]
        print(f"  {model_id:52}", end=" ", flush=True)
        result = attempt(model_id, key, schema, args.timeout, base_url)
        if result["ok"]:
            passed.append((model_id, result["seconds"]))
            print(f"OK  {result['seconds']:5.1f}s  commitments={result['commitments']}")
        else:
            print(f"--  {result['seconds']:5.1f}s  {result['why']}")

    print(f"\n{len(passed)} of {len(models)} produced a valid forced tool call.")
    for model_id, seconds in sorted(passed, key=lambda p: p[1]):
        print(f"  {seconds:5.1f}s  {model_id}")
    if not passed:
        print("\nNone. The forced-tool-call path cannot run on the free tier as configured;")
        print("a JSON-mode fallback in extract/client.py would be the next thing to try.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
