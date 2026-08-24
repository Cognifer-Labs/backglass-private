"""Which array to send, and saying so when none of it is answering.

The failure this holds the line on is measured. On 2026-08-21 the backend moved to
OpenRouter's free tier; over the two days that followed, 241 of 288 triage calls returned
`error`. Nothing escalated, nothing was reported, the backlog stayed at zero because rule
5 retries cleared it, and it was found two days later by hand-writing SQL. Three
properties keep that from being possible again, and each one is a test below:

  * the routed array does not dead-end — an escalation model holds the last slot;
  * three slots are three *distinct* models, which they were not;
  * a tier that is refusing is on the `backglass state` verdict list on the day.

And one property keeps the cure from being worse: a flap that has ended must not go on
demoting a model. The router's window is a day for exactly that reason, and the reporting
window is three days for the opposite one, so both are pinned here.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from backglass import modelhealth
from backglass.config import Settings
from backglass.extract.client import MAX_ROUTED_MODELS, DeepInfraBackend
from backglass.ledger import USER_ID

OPENROUTER = "https://openrouter.ai/api/v1"


def _calls(
    conn: sqlite3.Connection,
    model: str,
    *,
    tier: str = "triage",
    ok: int = 0,
    failed: int = 0,
    hours_ago: float = 1,
) -> None:
    at = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    conn.executemany(
        "INSERT INTO model_call (user_id, tier, model, prompt_chars, duration_ms,"
        " cost_usd, outcome, started_at) VALUES (?, ?, ?, 100, 1000, 0.0, ?, ?)",
        [(USER_ID, tier, model, "ok", at)] * ok
        + [(USER_ID, tier, model, "error", at)] * failed,
    )


def _backend(**kwargs: object) -> DeepInfraBackend:
    return DeepInfraBackend(api_key="k", base_url=OPENROUTER, **kwargs)  # type: ignore[arg-type]


class TestTheRoutedArray:
    def test_three_slots_are_three_distinct_models(self) -> None:
        """MODEL_EXTRACT is also the first entry of MODEL_FALLBACKS on the owner's
        install, so the extract array was sending the same model twice and a third of the
        fallback budget did not exist. OpenRouter rejects a fourth entry outright, so a
        wasted slot is a slot that cannot be bought back."""
        routed = _backend(fallbacks=("primary", "second", "third"))._routed("primary")
        assert routed == ["primary", "second", "third"]

    def test_without_an_escalation_model_the_body_is_what_it_always_was(self) -> None:
        """Opt-in by construction: an install that sets nothing must send byte-for-byte
        the array it sent before this existed."""
        assert _backend(fallbacks=("a", "b", "c"))._routed("p") == ["p", "a", "b"]

    def test_the_escalation_model_holds_the_last_slot(self) -> None:
        """The dead end is the bug. Every model in `fallbacks` is on the same free queue
        and they rate-limit together, so an `error` row means the whole array refused —
        there was nothing at the end of it that could answer."""
        routed = _backend(fallbacks=("a", "b", "c"), escalation="haiku")._routed("p")
        assert routed == ["p", "a", "haiku"]
        assert len(routed) == MAX_ROUTED_MODELS

    def test_it_survives_a_full_array_rather_than_being_truncated_off_it(self) -> None:
        """The case a trailing slice gets wrong, and it is the case that matters: the
        array is only full when there are plenty of free models to try, which is exactly
        when the escalation model would be dropped."""
        routed = _backend(
            fallbacks=("a", "b", "c", "d", "e"), escalation="haiku"
        )._routed("p")
        assert routed[-1] == "haiku"

    def test_it_is_not_sent_twice_when_it_is_already_configured(self) -> None:
        routed = _backend(fallbacks=("haiku", "b"), escalation="haiku")._routed("p")
        assert routed.count("haiku") == 1
        assert routed[-1] == "haiku"

    def test_a_measured_order_replaces_the_configured_one(self) -> None:
        routed = _backend(
            fallbacks=("a", "b"), routes={"p": ("b", "p", "a")}
        )._routed("p")
        assert routed == ["b", "p", "a"]

    def test_a_non_openrouter_base_url_gets_no_models_array_at_all(self) -> None:
        """DeepInfra has no such field and rejects unknown ones."""
        backend = DeepInfraBackend(
            api_key="k", base_url="https://api.deepinfra.com/v1/openai",
            fallbacks=("a",), escalation="haiku",
        )
        assert "openrouter" not in backend.base_url


class TestWhatCountsAsHealthy:
    def test_a_model_with_too_little_history_has_no_verdict(
        self, conn: sqlite3.Connection
    ) -> None:
        """`unknown`, not `ok` and not `broken` — the same rule `backglass state` applies
        to every probe that cannot run."""
        _calls(conn, "m", ok=1, failed=2)
        assert modelhealth.recent(conn)["m"].known is False

    def test_an_unknown_model_keeps_its_configured_position(
        self, conn: sqlite3.Connection
    ) -> None:
        """Absence of evidence is not failure. Demoting on it would freeze the configured
        order the first time a new model was added."""
        _calls(conn, "known-bad", ok=1, failed=20)
        health = modelhealth.recent(conn)
        assert modelhealth.rank(["untried", "known-bad"], health) == ["untried", "known-bad"]

    def test_a_refusing_model_stops_leading_the_array(
        self, conn: sqlite3.Connection
    ) -> None:
        _calls(conn, "bad", ok=2, failed=18)
        _calls(conn, "good", ok=20)
        assert modelhealth.rank(["bad", "good"], modelhealth.recent(conn)) == ["good", "bad"]

    def test_order_within_a_group_is_the_configured_one(
        self, conn: sqlite3.Connection
    ) -> None:
        """A stable partition, not a sort on rate: 2% versus 4% is not a reason to change
        what leads, and sorting on it would reshuffle the array on every unlucky call."""
        _calls(conn, "a", ok=39, failed=1)
        _calls(conn, "b", ok=40)
        assert modelhealth.rank(["a", "b"], modelhealth.recent(conn)) == ["a", "b"]

    def test_health_is_measured_over_the_tiers_the_array_will_serve(
        self, conn: sqlite3.Connection
    ) -> None:
        """The wrong answer this grouping exists to prevent. On the live ledger
        `nemotron-3-super` was failing half its extract calls and almost none of its
        relevance ones; pooled, it read as healthy and stayed at the head of the array."""
        _calls(conn, "m", tier="extract", ok=2, failed=18)
        _calls(conn, "m", tier="relevance", ok=100)

        pooled = modelhealth.recent(conn)["m"]
        scoped = modelhealth.recent(conn, tiers=("extract",))["m"]
        assert pooled.healthy is True
        assert scoped.healthy is False


class TestTheTwoWindows:
    def test_the_router_does_not_demote_over_a_flap_that_has_ended(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The whole reason the routing window is a day. These models flap: on 2026-08-21
        and 08-22 the triage model failed 78% of calls and by the 23rd it was at 5%.
        Reordering the array on the two bad days would be tuning against weather."""
        _calls(conn, "flapped", ok=10, failed=90, hours_ago=48)
        _calls(conn, "flapped", ok=40, hours_ago=1)

        assert modelhealth.recent(conn)["flapped"].healthy is True
        sett = settings.model_copy(
            update={"model_triage": "flapped", "model_extract": "flapped",
                    "model_fallbacks": ["other"]}
        )
        assert modelhealth.routes(conn, sett) == {}

    def test_the_report_still_shows_the_owner_that_yesterday_was_bad(
        self, conn: sqlite3.Connection
    ) -> None:
        """The opposite question, and it deserves the opposite window: "is it answering
        right now" is not "has this been working"."""
        _calls(conn, "flapped", ok=10, failed=90, hours_ago=48)
        _calls(conn, "flapped", ok=40, hours_ago=1)

        reported = {h.model: h for h in modelhealth.by_tier(conn)}
        assert reported["triage · flapped"].rate > modelhealth.UNHEALTHY_AT

    def test_neither_reader_sees_an_outage_older_than_its_window(
        self, conn: sqlite3.Connection
    ) -> None:
        """A count-bounded window would still be reporting this: a quiet tier's last 40
        calls can reach back a fortnight."""
        _calls(conn, "ancient", failed=100, hours_ago=24 * 30)

        assert "ancient" not in modelhealth.recent(conn)
        assert not [h for h in modelhealth.by_tier(conn) if "ancient" in h.model]

    def test_a_currently_broken_model_is_demoted(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """The other half of the flap pair, so the test above cannot pass by accident."""
        _calls(conn, "broken", ok=2, failed=38, hours_ago=1)
        _calls(conn, "works", ok=40, hours_ago=1)
        sett = settings.model_copy(
            update={"model_triage": "broken", "model_extract": "broken",
                    "model_fallbacks": ["works"]}
        )
        assert modelhealth.routes(conn, sett)["broken"] == ("works", "broken")


class TestTheStateVerdict:
    """The part that would have made 2026-08-21 visible on the day rather than two days
    later in a hand-written query. Both branches, because a check nobody has seen fail is
    a check nobody has seen work (lessons, 2026-07-30)."""

    def _verdict(self, conn: sqlite3.Connection, settings: Settings) -> object:
        from backglass import state as state_mod

        checks = state_mod.verdicts(state_mod.collect(conn, settings), conn, settings)
        return next(v for v in checks if v.name == "every model tier is answering")

    def test_it_names_the_tier_the_model_and_the_rate(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _calls(conn, "nano", tier="triage", ok=10, failed=30)
        verdict = self._verdict(conn, settings)

        assert verdict.ok is False  # type: ignore[attr-defined]
        assert "triage · nano" in verdict.detail  # type: ignore[attr-defined]
        assert "75%" in verdict.detail  # type: ignore[attr-defined]
        # The window is in the sentence: "failing 75%" with no window reads as "now",
        # and this one deliberately reaches back far enough to show a bad yesterday.
        assert f"last {modelhealth.REPORT_DAYS}d" in verdict.detail  # type: ignore[attr-defined]
        assert "MODEL_ESCALATION" in (verdict.remedy or "")  # type: ignore[attr-defined]

    def test_a_working_pipeline_passes(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _calls(conn, "nano", tier="triage", ok=40)
        _calls(conn, "big", tier="extract", ok=40)
        assert self._verdict(conn, settings).ok is True  # type: ignore[attr-defined]

    def test_an_empty_ledger_is_unknown_rather_than_ok(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        """Never a confident answer assembled from a missing input — `state`'s own rule."""
        verdict = self._verdict(conn, settings)
        assert verdict.unknown is True and verdict.ok is False  # type: ignore[attr-defined]

    def test_too_few_calls_to_judge_is_also_unknown(
        self, conn: sqlite3.Connection, settings: Settings
    ) -> None:
        _calls(conn, "nano", tier="triage", failed=3)
        assert self._verdict(conn, settings).unknown is True  # type: ignore[attr-defined]
