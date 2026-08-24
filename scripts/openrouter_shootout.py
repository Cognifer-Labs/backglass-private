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

CATALOGUE = "https://openrouter.ai/api/v1/models"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

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


def catalogue() -> list[dict]:
    request = urllib.request.Request(CATALOGUE, headers={"User-Agent": "backglass-shootout"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response).get("data", [])


def free_with_tools() -> list[dict]:
    return [
        model
        for model in catalogue()
        if model.get("id", "").endswith(":free")
        and "tools" in (model.get("supported_parameters") or [])
    ]


def attempt(model_id: str, key: str, schema: dict, timeout: int) -> dict:
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
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # OpenRouter attributes traffic by these; harmless and honest.
            "HTTP-Referer": "https://github.com/local/backglass",
            "X-Title": "Backglass shootout",
        },
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
    args = parser.parse_args()

    key = os.environ.get("MODEL_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or ""
    if not key:
        print("set MODEL_API_KEY (or OPENROUTER_API_KEY) to an OpenRouter key", file=sys.stderr)
        return 2

    schema = json_schema(TriageVerdict if args.triage else CommitmentExtraction)
    tier = "triage" if args.triage else "extract"
    models = free_with_tools()
    if args.only:
        models = [m for m in models if args.only in m["id"]]
    models.sort(key=lambda m: -(m.get("context_length") or 0))

    print(f"{len(models)} free model(s) advertising tools · schema = {tier}\n")
    passed: list[tuple[str, float]] = []
    for model in models:
        model_id = model["id"]
        print(f"  {model_id:52}", end=" ", flush=True)
        result = attempt(model_id, key, schema, args.timeout)
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
