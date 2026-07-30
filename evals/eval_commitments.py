"""Eval, not a test. Calls a live model. Never gates CI. See evals/README.md.

Runs the tier-2 prompt against the same seven fixtures the pipeline tests use, but with a
real model instead of the canned response — so `tests/test_commitments.py` measures the
post-processing and this measures the prompt.

Scored on the three things that actually break a brief:

  direction   an `i_owe` reported as `owed_to_me` is a commitment you think someone else
              owes you. Worse than missing it.
  due_at      CLAUDE.md rule 4. Checked against the fixture's expected date, which was
              computed from the message timestamp and never from today.
  count       inventing a commitment is the failure that destroys trust fastest, so the
              third-party fixture expecting zero is worth as much as the rest combined.

    uv run python evals/eval_commitments.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backglass.config import get_settings  # noqa: E402
from backglass.extract import prompts  # noqa: E402
from backglass.extract.client import ModelError, build  # noqa: E402
from backglass.extract.commitments import extract  # noqa: E402
from backglass.extract.dates import resolve_due  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "commitments"


def main() -> None:
    settings = get_settings()
    if not settings.owner_emails:
        print("OWNER_EMAILS is empty; the prompt needs it to decide direction. Set it in .env.")
        return

    client = build(settings)
    prompt = prompts.load("extract-commitments")
    spend = 0.0
    scored = 0
    correct_count = correct_direction = correct_date = 0

    print(f"\nextraction prompt {prompt.stamp}   model={settings.model_extract}\n")

    for path in sorted(FIXTURES.glob("*.json")):
        case = json.loads(path.read_text())
        message = case["message"]
        occurred_at = _occurred_at(message["date"])
        item = {
            "author": message["from"],
            "raw_json": json.dumps(
                {"headers": {"To": message.get("to", ""), "Cc": message.get("cc", "")}}
            ),
            "occurred_at": occurred_at,
            "title": message["subject"],
            "body_text": message["body"],
        }
        try:
            result, cost = extract(
                item,
                prompt=prompt,
                client=client,
                model=settings.model_extract,
                budget_usd=settings.per_call_budget_usd,
                settings=settings,
            )
        except ModelError as exc:
            print(f"  {path.stem:<32} ERROR  {str(exc)[:70]}")
            continue

        spend += cost
        scored += 1
        expected = case["expect"]
        expected_n = expected["inserted"] + expected["deduped"]
        got_n = len(result.commitments)

        count_ok = got_n == expected_n
        correct_count += count_ok

        expected_dates = [d for d in expected["due_at"] if d]
        got_dates = [
            resolve_due(c.due_at, occurred_at=occurred_at).value for c in result.commitments
        ]
        date_ok = all(d in got_dates for d in expected_dates)
        correct_date += date_ok

        gold_direction = _gold_direction(case)
        directions = {c.direction for c in result.commitments}
        direction_ok = gold_direction is None or directions == {gold_direction}
        correct_direction += direction_ok

        marks = "".join(("." if ok else "X") for ok in (count_ok, direction_ok, date_ok))
        print(
            f"  {path.stem:<32} {marks}  n={got_n}/{expected_n}  "
            f"dates={got_dates} want={expected_dates}"
        )
        for commitment in result.commitments:
            print(
                f"      {commitment.direction:<11} {commitment.what[:38]:<38} "
                f"conf {commitment.confidence:.2f}"
            )

    if not scored:
        print("\n  nothing scored\n")
        return

    print(
        f"\n  count {correct_count}/{scored}   direction {correct_direction}/{scored}   "
        f"dates {correct_date}/{scored}   spend ${spend:.4f}"
    )
    print("  legend: count/direction/date, '.' pass 'X' fail")
    print("\n  Not pass/fail. Compare against the previous prompt version.\n")


def _gold_direction(case: dict[str, object]) -> str | None:
    response = case.get("model_response") or {}
    listed = response.get("commitments") or []  # type: ignore[union-attr]
    directions = {c["direction"] for c in listed}
    return directions.pop() if len(directions) == 1 else None


def _occurred_at(header_date: str) -> str:
    from email.utils import parsedate_to_datetime

    return parsedate_to_datetime(header_date).replace(microsecond=0).isoformat()


if __name__ == "__main__":
    main()
