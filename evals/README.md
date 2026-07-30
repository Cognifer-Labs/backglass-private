# Evals

**These are not tests.** They call a live model, they are not pass/fail, and they never
run in CI.

docs/10 §Testing: *"Evals actually call the model against golden fixtures and report
precision and recall per prompt version. These do not run in CI, are not pass/fail, and
exist so that changing a prompt produces a number you can compare. Confusing an eval with
a test is how a nondeterministic system ends up with a flaky suite that people learn to
ignore."*

Three mechanisms keep them out of the suite, so that confusion has to be deliberate:

1. `pyproject.toml` sets `testpaths = ["tests"]`, so `pytest` never looks here.
2. Nothing in this directory is named `test_*`.
3. They are run as scripts, not collected.

```
uv run python evals/eval_triage.py
uv run python evals/eval_commitments.py
```

Each prints a table per prompt version. Compare the numbers across a prompt change; do
not gate anything on them.

Golden labels live next to the fixtures they judge, in `tests/fixtures/commitments/`
(the `expect` block) and in `evals/golden_triage.json`.
