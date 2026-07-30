"""Eval, not a test. Calls a live model. Never gates CI. See evals/README.md.

Reports precision and recall for the tier-1 triage prompt against hand-labelled golden
messages, so that changing triage.md produces a number you can compare rather than a
feeling.

The asymmetry matters more than the headline accuracy: triage.md says a false negative
loses a commitment permanently while a false positive costs one extraction call. Read the
recall column first. A prompt change that lifts precision by dropping recall is a
regression even though it looks like an improvement.

    uv run python evals/eval_triage.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backglass.config import get_settings  # noqa: E402
from backglass.extract import prompts  # noqa: E402
from backglass.extract.client import ModelError, build  # noqa: E402
from backglass.extract.triage import triage  # noqa: E402

GOLDEN = Path(__file__).parent / "golden_triage.json"


def main() -> None:
    settings = get_settings()
    client = build(settings)
    prompt = prompts.load("triage")
    cases = json.loads(GOLDEN.read_text())

    tp = fp = tn = fn = 0
    spend = 0.0
    rows: list[tuple[str, str, str, str]] = []

    for case in cases:
        item = {
            "author": case["author"],
            "occurred_at": case["occurred_at"],
            "title": case["title"],
            "body_text": case["body"],
        }
        try:
            outcome = triage(
                item,
                prompt=prompt,
                client=client,
                model=settings.model_triage,
                budget_usd=settings.per_call_budget_usd,
            )
        except ModelError as exc:
            rows.append((case["label"], "ERROR", "-", str(exc)[:60]))
            continue

        spend += outcome.cost_usd
        predicted = outcome.verdict == "keep"
        expected = bool(case["keep"])
        if predicted and expected:
            tp += 1
            mark = "ok"
        elif predicted and not expected:
            fp += 1
            mark = "FP"
        elif not predicted and expected:
            fn += 1
            mark = "FN  <- lost a commitment"
        else:
            tn += 1
            mark = "ok"
        rows.append((case["label"], "keep" if predicted else "drop", mark, outcome.reason[:60]))

    width = max(len(row[0]) for row in rows)
    print(f"\ntriage prompt {prompt.stamp}   model={settings.model_triage}\n")
    for label, verdict, mark, reason in rows:
        print(f"  {label:<{width}}  {verdict:<5} {mark:<26} {reason}")

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    kill_rate = (tn + fn) / len(cases) if cases else 0.0
    print(
        f"\n  precision {precision:.2f}   recall {recall:.2f}   "
        f"kill rate {kill_rate:.0%}   spend ${spend:.4f}"
    )
    print(f"  tp={tp} fp={fp} tn={tn} fn={fn}")
    if fn:
        print(f"\n  {fn} false negative(s). Each is a commitment the owner never hears about.")
    print("\n  Not pass/fail. Compare against the previous prompt version.\n")


if __name__ == "__main__":
    main()
