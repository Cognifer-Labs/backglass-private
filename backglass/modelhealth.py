"""Which models are actually answering, read from the calls already recorded.

On 2026-08-21 the backend moved to OpenRouter's free tier. Over the two days that
followed, 241 of 288 triage calls failed. Nothing escalated, nothing was reported, and
the only way anyone could have seen it was to write SQL against `model_call` — which is
how it was eventually found, two days late. The backlog stayed at zero the whole time
because rule 5 retries cleared it, so every surface the owner looks at said the pipeline
was healthy while it was burning four calls for every one that landed.

It then recovered on its own, which is the actual lesson. A free tier is a queue, models
flap, and the next flap costs another two days unless two things are true: something
reports it, and the routed array does not dead-end.

**What "health" means here, precisely.** `model_call.model` records the model this
pipeline *asked* for, and OpenRouter routes the request itself when the body carries a
`models` array (`extract/client.py`). So a row is the outcome of the whole array headed by
that model, not a verdict on that model alone — and an `error` row means every model in
the array refused, which is what makes the free tier's shared-queue behaviour visible at
all. Ranking on it is still the right call: what is being chosen is which array to send.

Read-only, no migration, no new table. `telemetry.py` already writes everything this
needs, and a second store of the same facts would drift from the first.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from backglass.config import Settings
from backglass.ledger import USER_ID

#: A ceiling on rows per model, not the window — `RECENT_DAYS` below is what actually
#: bounds "recent", and this only stops a backfill of thousands of calls from being read
#: in full to answer a yes/no question. It has to stay well above normal volume (~300
#: triage calls in three days) or it becomes the binding constraint and the two readers
#: below start disagreeing: a count cap makes a busy tier's window shorter in *time* than
#: a quiet one's, so the same outage is current to one and expired to the other. That is
#: not hypothetical — at 40 it happened, between the router and `backglass state`, on the
#: first real read.
WINDOW = 200

#: Below this many recent calls there is no opinion to have. A model that has answered
#: twice is not "100% healthy", and a model that has failed twice is not broken — the
#: honest answer is `unknown`, which is what `backglass state` says everywhere else when
#: a probe cannot run.
MIN_CALLS = 8

#: Two windows, because there are two questions and they are not the same question.
#:
#: The router asks *what is answering right now*, and these models flap on a timescale of
#: about a day — so a day is the window in which an outage is evidence about the next
#: call. Reading three days would have demoted both primaries on 2026-08-23 over an
#: outage that ended on the 22nd, which is precisely the mistake of tuning against a flap.
#:
#: `backglass state` asks *has this been working*, and a person wants to know that
#: yesterday was bad even though this morning is fine. Three days.
#:
#: What is not allowed is one stated window with two effective ones. Bounded by call count
#: alone, a busy tier's window is shorter in time than a quiet tier's, so the same outage
#: is current to one reader and expired to the other — which is what happened here on the
#: first real read, with the verdict reporting 53% while the router saw the same model as
#: healthy. Time bounds both; `WINDOW` is only a ceiling.
ROUTING_DAYS = 1
REPORT_DAYS = 3

#: The failure rate at which a model stops leading the array. Deliberately not near zero:
#: the free tier's normal is a few percent, and demoting on noise would make the order
#: flap as hard as the models do. At a quarter, something is wrong.
UNHEALTHY_AT = 0.25


def _since(days: int) -> str:
    """The floor both reads share, as the ISO prefix `started_at` is written with.

    `datetime.now(UTC)` rather than an injected clock because this is a health probe over
    wall-clock telemetry, not a ledger write — nothing here resolves a relative date
    against a source item (rule 4), and every caller is asking "right now".
    """
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


@dataclass(frozen=True)
class Health:
    """One model's recent record. `rate` is undefined below `MIN_CALLS` — ask `known`."""

    model: str
    calls: int
    failures: int

    @property
    def known(self) -> bool:
        return self.calls >= MIN_CALLS

    @property
    def rate(self) -> float:
        return self.failures / self.calls if self.calls else 0.0

    @property
    def healthy(self) -> bool:
        """Unknown counts as healthy. A model nobody has tried is not a model that failed,
        and demoting it on absence of evidence would freeze the configured order the first
        time a new model was added."""
        return not self.known or self.rate < UNHEALTHY_AT


#: Which tiers each configured model serves, from the call sites that pass it:
#: `sync.py:769` and `:833` (triage, batch triage) send `model_triage`; `sync.py:953`,
#: `extract/recheck.py:347` and `extract/relevance.py:327` send `model_extract`.
#:
#: This grouping is not bookkeeping, it is the difference between a right and a wrong
#: answer. Measured on the live ledger on 2026-08-23, `nemotron-3-super` was failing 32%
#: of extract calls and 53% of recheck calls — and 1 in 40 when every tier it serves was
#: pooled together, because the tiers where it is fine drowned the ones where it is not.
#: A router that reads the pooled number leaves a broken model at the head of the array
#: and reports it healthy.
TIERS_FOR_TRIAGE = ("triage", "triage_batch")
TIERS_FOR_EXTRACT = ("extract", "recheck", "relevance")


def recent(
    conn: sqlite3.Connection,
    *,
    tiers: tuple[str, ...] | None = None,
    window: int = WINDOW,
    days: int = ROUTING_DAYS,
) -> dict[str, Health]:
    """Per model, over that model's own last `window` calls in `tiers` — not the last
    `window` calls overall, which a busy tier would otherwise monopolise.

    `tiers=None` pools every tier and is for reporting a model's general record. Routing
    always passes the tiers the array will actually serve; see the note above.
    """
    named = ", ".join(f":t{i}" for i in range(len(tiers or ())))
    where = "WHERE user_id = :user_id AND started_at >= :since" + (
        f" AND tier IN ({named})" if tiers else ""
    )
    rows = conn.execute(
        f"""
        SELECT model,
               COUNT(*) AS calls,
               SUM(CASE WHEN outcome != 'ok' THEN 1 ELSE 0 END) AS failures
        FROM (
            SELECT model, outcome,
                   ROW_NUMBER() OVER (PARTITION BY model ORDER BY id DESC) AS rn
            FROM model_call {where}
        )
        WHERE rn <= :window
        GROUP BY model
        """,
        {
            "user_id": USER_ID,
            "window": window,
            "since": _since(days),
            **{f"t{i}": t for i, t in enumerate(tiers or ())},
        },
    ).fetchall()
    return {
        str(r["model"]): Health(str(r["model"]), int(r["calls"]), int(r["failures"] or 0))
        for r in rows
    }


def by_tier(
    conn: sqlite3.Connection, *, window: int = WINDOW, days: int = REPORT_DAYS
) -> list[Health]:
    """The same read, split by tier, for reporting rather than for routing.

    Routing asks "which array should I send", which is a question about a model. A person
    reading `backglass state` asks "is triage working", which is a question about a tier —
    and the two came apart on 2026-08-21, when `triage_batch` failed at a different rate
    from single triage on the same model.
    """
    rows = conn.execute(
        """
        SELECT tier || ' · ' || model AS model,
               COUNT(*) AS calls,
               SUM(CASE WHEN outcome != 'ok' THEN 1 ELSE 0 END) AS failures
        FROM (
            SELECT tier, model, outcome,
                   ROW_NUMBER() OVER (
                       PARTITION BY tier, model ORDER BY id DESC
                   ) AS rn
            FROM model_call WHERE user_id = :user_id AND started_at >= :since
        )
        WHERE rn <= :window
        GROUP BY tier, model
        ORDER BY 1
        """,
        {"user_id": USER_ID, "window": window, "since": _since(days)},
    ).fetchall()
    return [
        Health(str(r["model"]), int(r["calls"]), int(r["failures"] or 0)) for r in rows
    ]


def rank(models: list[str], health: dict[str, Health]) -> list[str]:
    """The same models, unhealthy ones last, configured order preserved within each group.

    A stable partition rather than a sort on rate, and that is the whole design. Sorting
    by measured rate would reshuffle the array every time one call landed differently,
    and the difference between 2% and 4% is not a reason to change what leads. What the
    array should express is the author's preference, with anything currently broken moved
    out of the way — so a model with no history keeps exactly the position it was given.
    """
    seen: set[str] = set()
    ordered = [m for m in models if m and not (m in seen or seen.add(m))]
    well = [m for m in ordered if health.get(m, Health(m, 0, 0)).healthy]
    ill = [m for m in ordered if m not in well]
    return well + ill


def routes(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    window: int = WINDOW,
    days: int = ROUTING_DAYS,
) -> dict[str, tuple[str, ...]]:
    """For each configured primary, the order to send. Empty when nothing needs reordering.

    Empty is not a degenerate case, it is the normal one: on a healthy day `rank` returns
    the configured order and this returns `{}`, so the backend sends the body it would
    have sent anyway. Nothing about the request changes until something is measurably
    wrong.

    "The configured order" is itself `rank`ed against no health, so a config where the
    primary also appears in the fallbacks — which is what `MODEL_EXTRACT` plus
    `MODEL_FALLBACKS` currently is — is compared after deduplication rather than before.
    Otherwise removing a duplicate would register as a reorder and every day would look
    like a bad one.
    """
    out: dict[str, tuple[str, ...]] = {}
    for primary, tiers in (
        (settings.model_triage, TIERS_FOR_TRIAGE),
        (settings.model_extract, TIERS_FOR_EXTRACT),
    ):
        if not primary:
            continue
        health = recent(conn, tiers=tiers, window=window, days=days)
        configured = rank([primary, *settings.model_fallbacks], {})
        ranked = rank([primary, *settings.model_fallbacks], health)
        if ranked != configured:
            out[primary] = tuple(ranked)
    return out
