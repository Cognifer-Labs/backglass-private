"""One row per model call. Phase 0 of the backend plan.

`run.spend_cents` is a single per-run total that mixes triage and extraction, so it
cannot answer what a call costs against its payload, where a 90-second sync's wall-clock
goes, or whether per-call session overhead dominates on the CLI backend. Every cost
argument in this project has been reasoning from one measurement taken on 2026-07-30 in
a different context. This module is how the next one gets its own.

The wrapper is deliberately the thinnest thing that can see a whole call: it times
`complete`, records the payload size, and records the *outcome* including the failures,
because a call that rate-limited still took time and a call that parked an item still
cost something. It changes no backend and no result.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from backglass.db import now_iso
from backglass.extract.client import (
    ModelAuthError,
    ModelClient,
    ModelResult,
    RateLimited,
)
from backglass.ledger import USER_ID


@dataclass(frozen=True)
class CallRecord:
    tier: str
    model: str
    prompt_chars: int
    duration_ms: int
    cost_usd: float
    outcome: str  # ok | error | rate_limited | auth
    started_at: str


class Metered:
    """A `ModelClient` that appends a `CallRecord` per call to `sink`.

    One instance per pass, because the pass is what knows the tier — `sync` wraps the
    client separately for triage, batch triage and extraction rather than making the
    wrapper guess from a schema.

    `sink` is appended to from the extraction pool's worker threads. `list.append` is
    atomic under the GIL, and nothing here reads the list until the pool has joined, so
    no lock is needed; if either of those stops being true this needs one.
    """

    def __init__(self, inner: ModelClient, tier: str, sink: list[CallRecord]) -> None:
        self._inner = inner
        self._tier = tier
        self._sink = sink
        #: Forwarded, never assumed, and a plain attribute rather than a property
        #: because `ModelClient` declares it settable. SpendCap reads this off whatever
        #: client it is handed, so a wrapper that answered for itself would turn a
        #: subscription backend into a billed one and reinstate the 2026-08-03 freeze.
        self.spend_is_imputed: bool = bool(getattr(inner, "spend_is_imputed", False))

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        model: str,
        budget_usd: float,
    ) -> ModelResult:
        started = now_iso()
        clock = time.monotonic()
        outcome = "error"
        cost = 0.0
        try:
            result = self._inner.complete(
                system=system, user=user, schema=schema, model=model, budget_usd=budget_usd
            )
        except RateLimited as exc:
            outcome = "rate_limited"
            cost = float(getattr(exc, "cost_usd", 0.0) or 0.0)
            raise
        except ModelAuthError:
            outcome = "auth"
            raise
        except Exception as exc:  # ModelError and anything a backend lets through
            cost = float(getattr(exc, "cost_usd", 0.0) or 0.0)
            raise
        else:
            outcome = "ok"
            cost = result.cost_usd
            return result
        finally:
            # `system` counts too: on a backend that re-sends it per call — which the CLI
            # does, in a fresh process every time — the static half is exactly the payload
            # this measurement exists to size.
            self._sink.append(
                CallRecord(
                    tier=self._tier,
                    model=model,
                    prompt_chars=len(system) + len(user),
                    duration_ms=int((time.monotonic() - clock) * 1000),
                    cost_usd=cost,
                    outcome=outcome,
                    started_at=started,
                )
            )


def write_calls(
    conn: sqlite3.Connection, calls: list[CallRecord], run_id: int | None
) -> int:
    """Persist a run's calls. Written once, after the run row exists."""
    if not calls:
        return 0
    conn.executemany(
        "INSERT INTO model_call (user_id, run_id, tier, model, prompt_chars,"
        " duration_ms, cost_usd, outcome, started_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                USER_ID,
                run_id,
                c.tier,
                c.model,
                c.prompt_chars,
                c.duration_ms,
                c.cost_usd,
                c.outcome,
                c.started_at,
            )
            for c in calls
        ],
    )
    return len(calls)
