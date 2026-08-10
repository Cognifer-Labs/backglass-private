"""A rejected credential is an outage, not a bad answer.

2026-08-05, runs 111-118: every model call in six consecutive scheduled syncs came back
`Failed to authenticate: OAuth session expired and could not be refreshed`, and every one
of them was recorded as "model returned an unusable response after 2 attempts" — the same
sentence a garbled JSON response produces. Nothing in the run row said the items had not
been read at all, and each one cost two CLI subprocesses to learn the same thing twice.

These tests pin the three behaviours that failure should have had:
  - an authentication failure is its own error type, named as such in `run.errors_json`;
  - it is never retried, because restating the schema cannot fix a rejected session;
  - the first one stops the rest of the run from re-asking a backend that already said no.

Plus the one behaviour that keeps it from happening in the first place: a parallel pass
opens with a single call, so concurrent workers never race each other's token refresh.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from backglass.config import Settings
from backglass.extract.client import (
    AuthCircuit,
    ClaudeCLIBackend,
    DeepInfraBackend,
    ModelAuthError,
    ModelError,
    ModelResult,
    build,
)
from backglass.extract import client
from backglass.sync import _in_parallel, sync
from tests.conftest import FakeGmailService, gmail_message, make_connector

TRIAGE_SCHEMA = {"type": "object", "properties": {"keep": {}, "reason": {}}}

#: Verbatim from run 118's errors_json.
OAUTH_MESSAGE = (
    "Failed to authenticate: OAuth session expired and could not be refreshed"
)


def _cli_envelope(result: str, *, is_error: bool = True) -> str:
    import json

    return json.dumps({"is_error": is_error, "result": result, "total_cost_usd": 0.003})


class _FakeRun:
    """Stands in for subprocess.run; counts how many CLI processes were spawned."""

    def __init__(self, stdout: str, *, returncode: int = 0, stderr: str = ""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr
        self.calls = 0

    def __call__(self, command: list[str], **kwargs: Any) -> Any:
        self.calls += 1
        return SimpleNamespace(
            stdout=self.stdout, stderr=self.stderr, returncode=self.returncode
        )


class TestTheErrorSaysWhichKindOfFailure:
    def test_oauth_expiry_is_an_auth_error_not_a_bad_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _FakeRun(_cli_envelope(OAUTH_MESSAGE))
        monkeypatch.setattr("backglass.extract.client.subprocess.run", runner)

        with pytest.raises(ModelAuthError) as caught:
            ClaudeCLIBackend().complete(
                system="s", user="u", schema=TRIAGE_SCHEMA,
                model="haiku", budget_usd=0.1,
            )

        # The sentence that reaches run.errors_json has to name the cause. "unusable
        # response" was what it said for six hours while nothing was being read.
        assert "authentication failed" in str(caught.value)
        assert "unusable response" not in str(caught.value)

    def test_a_rejected_session_on_stderr_is_also_an_auth_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _FakeRun("", returncode=1, stderr=f"error: {OAUTH_MESSAGE}")
        monkeypatch.setattr("backglass.extract.client.subprocess.run", runner)

        with pytest.raises(ModelAuthError):
            ClaudeCLIBackend().complete(
                system="s", user="u", schema=TRIAGE_SCHEMA,
                model="haiku", budget_usd=0.1,
            )

    def test_a_garbled_response_is_still_an_ordinary_model_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half of the distinction: junk must not be read as an outage."""
        runner = _FakeRun("this is not json at all")
        monkeypatch.setattr("backglass.extract.client.subprocess.run", runner)

        with pytest.raises(ModelError) as caught:
            ClaudeCLIBackend().complete(
                system="s", user="u", schema=TRIAGE_SCHEMA,
                model="haiku", budget_usd=0.1,
            )
        assert not isinstance(caught.value, ModelAuthError)

    def test_deepinfra_401_is_an_auth_error(self) -> None:
        import urllib.error

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)  # type: ignore[arg-type]

        backend = DeepInfraBackend(api_key="k")
        import backglass.extract.client as client_mod

        original = client_mod.urllib.request.urlopen
        client_mod.urllib.request.urlopen = boom  # type: ignore[assignment]
        try:
            with pytest.raises(ModelAuthError):
                backend.complete(
                    system="s", user="u", schema=TRIAGE_SCHEMA,
                    model="m", budget_usd=0.1,
                )
        finally:
            client_mod.urllib.request.urlopen = original  # type: ignore[assignment]


class TestARejectedSessionIsNotRetried:
    def test_auth_failure_spawns_one_process_not_two(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """extract-commitments.md's retry is for malformed JSON. A rejected session is
        identical on the second ask, so the second subprocess buys nothing — and on
        2026-08-05 it doubled 22 doomed calls into 44."""
        runner = _FakeRun(_cli_envelope(OAUTH_MESSAGE))
        monkeypatch.setattr("backglass.extract.client.subprocess.run", runner)

        with pytest.raises(ModelAuthError):
            ClaudeCLIBackend(attempts=2).complete(
                system="s", user="u", schema=TRIAGE_SCHEMA,
                model="haiku", budget_usd=0.1,
            )
        assert runner.calls == 1

    def test_a_malformed_response_still_gets_its_second_chance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guard on the other side: the documented retry must survive this change."""
        runner = _FakeRun(_cli_envelope("not json", is_error=False))
        monkeypatch.setattr("backglass.extract.client.subprocess.run", runner)

        with pytest.raises(ModelError):
            ClaudeCLIBackend(attempts=2).complete(
                system="s", user="u", schema=TRIAGE_SCHEMA,
                model="haiku", budget_usd=0.1,
            )
        assert runner.calls == 2


class _CountingBackend:
    spend_is_imputed = True

    def __init__(self, error: Exception):
        self.error, self.calls = error, 0

    def complete(self, **kwargs: Any) -> ModelResult:
        self.calls += 1
        raise self.error


class TestOneRefusalIsEnoughForTheWholeRun:
    def test_the_circuit_stops_calling_a_backend_that_said_no(self) -> None:
        inner = _CountingBackend(ModelAuthError("authentication failed: expired"))
        circuit = AuthCircuit(inner)

        for _ in range(5):
            with pytest.raises(ModelAuthError):
                circuit.complete(
                    system="s", user="u", schema=TRIAGE_SCHEMA,
                    model="haiku", budget_usd=0.1,
                )

        # Five items still each raise — nothing is swallowed, every one stays visible in
        # run.errors_json — but only the first one cost a real call.
        assert inner.calls == 1

    def test_an_ordinary_failure_does_not_trip_the_circuit(self) -> None:
        """A junk response says nothing about the next item, so the run continues."""
        inner = _CountingBackend(ModelError("no structured_output"))
        circuit = AuthCircuit(inner)

        for _ in range(3):
            with pytest.raises(ModelError):
                circuit.complete(
                    system="s", user="u", schema=TRIAGE_SCHEMA,
                    model="haiku", budget_usd=0.1,
                )
        assert inner.calls == 3

    def test_the_circuit_reports_the_wrapped_backends_spend_kind(self) -> None:
        """The spend cap reads this through the wrapper; a wrong answer here would let
        an imputed subscription price stop real work (the 2026-08-03 failure)."""
        assert AuthCircuit(_CountingBackend(ModelError("x"))).spend_is_imputed is True

    def test_build_wraps_the_backend_so_every_caller_gets_it(
        self, settings: Settings
    ) -> None:
        assert isinstance(build(settings), AuthCircuit)


class TestTheFirstCallGoesAlone:
    def test_a_parallel_pass_opens_with_a_single_call(self) -> None:
        """Six processes that all start on an expired subscription token each try to
        refresh it. Letting one go first means at most one refresh is ever in flight."""
        lock = threading.Lock()
        events: list[str] = []

        def work(item: int) -> int:
            with lock:
                events.append(f"start{item}")
            time.sleep(0.05)
            with lock:
                events.append(f"end{item}")
            return item

        cap = SimpleNamespace(reached=False)
        assert list(_in_parallel(work, list(range(7)), 3, cap, stop_on_cap=False)) == list(
            range(7)
        )

        # The lead call finishes before anything else begins.
        assert events[0] == "start0"
        assert events[1] == "end0"

        # ...and concurrency survives: the next wave's three calls all begin before any
        # of them ends. Compared as a set — which of the three wins the race is the
        # scheduler's business, that they overlap at all is the point.
        assert set(events[2:5]) == {"start1", "start2", "start3"}


class TestTheRunSaysItCouldNotAuthenticate:
    def test_sync_marks_an_auth_outage_distinctly(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Any
    ) -> None:
        """The whole point: a run that read nothing must not look like a run that read
        everything and disliked it."""
        connector = make_connector(
            [
                gmail_message(
                    {
                        "id": "m1",
                        "from": "Dana <dana@example.gov>",
                        "to": "alex.rivera@example.com",
                        "subject": "Migration plan",
                        "date": "Fri, 10 Jul 2026 09:15:00 -0700",
                        "body": "I'll send the revised plan by Friday.",
                    }
                )
            ],
            boundary,
        )

        class DeadSession:
            spend_is_imputed = True

            def complete(self, **kwargs: Any) -> ModelResult:
                raise ModelAuthError(f"authentication failed: {OAUTH_MESSAGE}")

        report = sync(conn, settings, [connector], AuthCircuit(DeadSession()))

        assert report.model_auth_failed is True
        assert report.exit_code == 1
        joined = " | ".join(report.errors)
        assert "authentication failed" in joined
        # The old sentence claimed the model answered. It had not been asked.
        assert "unusable response" not in joined

    def test_a_healthy_run_is_not_marked(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Any
    ) -> None:
        from tests.conftest import FakeModel

        connector = make_connector(
            [
                gmail_message(
                    {
                        "id": "m1",
                        "from": "Dana <dana@example.gov>",
                        "to": "alex.rivera@example.com",
                        "subject": "Migration plan",
                        "date": "Fri, 10 Jul 2026 09:15:00 -0700",
                        "body": "I'll send the revised plan by Friday.",
                    }
                )
            ],
            boundary,
        )
        report = sync(conn, settings, [connector], FakeModel())
        assert report.model_auth_failed is False


class TestAnOutageAndAClosedWindowStaySeparate:
    """The two pauses this codebase now knows about must never be told as each other.

    A usage window reopens on its own, so its items stay PENDING, spend no attempt, and
    the run promises a retry. A rejected credential reopens when a human fixes it, so its
    items are parked and the run has to say so. Swapping the sentences would either
    promise a retry that never comes or send the owner to re-authenticate a session that
    was fine.
    """

    def test_the_circuit_lets_a_rate_limit_through_untouched(self) -> None:
        """AuthCircuit wraps every backend, so a shut window passes through it on its way
        to _LimitClaim. It must arrive as itself, and must not trip the auth circuit."""
        from backglass.extract.client import RateLimited

        inner = _CountingBackend(RateLimited("model rate limit: Claude usage limit reached"))
        circuit = AuthCircuit(inner)

        for _ in range(3):
            with pytest.raises(RateLimited) as caught:
                circuit.complete(
                    system="s", user="u", schema=TRIAGE_SCHEMA,
                    model="haiku", budget_usd=0.1,
                )
            # Not re-wrapped as an outage on the way out.
            assert not isinstance(caught.value, ModelAuthError)

        # The circuit stayed closed: a limit is not evidence about our credentials, so
        # every call still reaches the backend and the window can be found to have
        # reopened.
        assert inner.calls == 3

    def test_auth_wins_a_sentence_that_reads_as_both(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The vocabularies look disjoint, but the tie-break is a decision, not luck: a
        false rate limit stops the wave and spends no attempt, which is the shape that
        stalls a queue. A false auth error only parks. Ties go to the bounded failure."""
        both = "Failed to authenticate: OAuth session expired; usage limit reached"
        runner = _FakeRun(_cli_envelope(both))
        monkeypatch.setattr("backglass.extract.client.subprocess.run", runner)

        with pytest.raises(ModelAuthError):
            ClaudeCLIBackend().complete(
                system="s", user="u", schema=TRIAGE_SCHEMA,
                model="haiku", budget_usd=0.1,
            )

    def test_the_two_runs_do_not_borrow_each_others_flags(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Any
    ) -> None:
        """The interaction itself: same pipeline, same items, two refusals, and the two
        SyncReports have to disagree about everything that matters."""
        from backglass.extract.client import RateLimited
        from tests.conftest import FakeModel

        specs = [
            {
                "id": f"m{n}",
                "from": f"Dana{n} <d{n}@example.gov>",
                "to": "alex.rivera@example.com",
                "subject": f"Migration plan {n}",
                "date": "Fri, 10 Jul 2026 09:15:00 -0700",
                "body": f"I'll have plan {n} to you by Friday.",
            }
            for n in range(3)
        ]
        messages = [gmail_message(s) for s in specs]

        class ShutWindow(FakeModel):
            def complete(self, **kwargs: Any) -> ModelResult:
                if "keep" in kwargs["schema"].get("properties", {}):
                    raise RateLimited("model rate limit: Claude usage limit reached")
                return super().complete(**kwargs)

        class DeadSession(FakeModel):
            def complete(self, **kwargs: Any) -> ModelResult:
                if "keep" in kwargs["schema"].get("properties", {}):
                    raise ModelAuthError(f"authentication failed: {OAUTH_MESSAGE}")
                return super().complete(**kwargs)

        limited = sync(conn, settings, [make_connector(messages, boundary)], ShutWindow())

        # A fresh ledger, so the auth run meets the same items in the same state.
        import backglass.db as db_mod

        conn2 = db_mod.connect(settings.db_path.parent / "second.db")
        db_mod.migrate(conn2)
        outage = sync(
            conn2,
            settings,
            [make_connector([gmail_message(s) for s in specs], boundary)],
            AuthCircuit(DeadSession()),
        )

        # A closed window: the run is degraded, says which stage, and promises a retry.
        assert (limited.rate_limited, limited.degraded) == (True, True)
        assert limited.degrade_reason == "rate_limit:triage"
        assert limited.model_auth_failed is False

        # An outage: no degrade_reason at all, because nothing here reopens by itself.
        assert outage.model_auth_failed is True
        assert outage.rate_limited is False
        assert outage.rate_limited_stage is None
        assert outage.degrade_reason is None

        # And the sentences the owner reads differ.
        assert "authentication failed" in " | ".join(outage.errors)
        assert "authentication failed" not in " | ".join(limited.errors)
        assert "rate limit" not in " | ".join(outage.errors)

    def test_an_auth_failure_is_never_a_limit_claim(
        self, conn: sqlite3.Connection, settings: Settings, boundary: Any
    ) -> None:
        """Two auth failures in one pass are two parked items, not a corroborated wall.
        _LimitClaim's second claimant confirms a window; an outage has no window."""
        from tests.conftest import FakeModel

        messages = [
            gmail_message(
                {
                    "id": f"m{n}",
                    "from": f"Dana{n} <d{n}@example.gov>",
                    "to": "alex.rivera@example.com",
                    "subject": f"Plan {n}",
                    "date": "Fri, 10 Jul 2026 09:15:00 -0700",
                    "body": f"I'll have plan {n} to you by Friday.",
                }
            )
            for n in range(4)
        ]

        class DeadSession(FakeModel):
            def complete(self, **kwargs: Any) -> ModelResult:
                if "keep" in kwargs["schema"].get("properties", {}):
                    raise ModelAuthError(f"authentication failed: {OAUTH_MESSAGE}")
                return super().complete(**kwargs)

        report = sync(
            conn, settings, [make_connector(messages, boundary)], AuthCircuit(DeadSession())
        )

        assert report.model_auth_failed is True
        assert report.rate_limited is False
        assert report.degrade_reason is None
        # Every item accounted for, none of them silently swallowed by a stopped wave.
        assert len([e for e in report.errors if "triage" in e]) == 4


assert FakeGmailService  # imported for the connector fixtures above


class TestBringYourOwnEndpoint:
    """`openai_compatible`: the same dialect, pointed anywhere.

    Ollama and vLLM on localhost, Together, Groq, OpenRouter, DeepInfra — they differ by
    a URL and a key, not by code, because they all speak the chat-completions shape the
    backend already forces a tool call through. Naming each one in a Literal and adding a
    branch per provider would have been a new class every time somebody changed vendor.

    The reason this matters here specifically: `claude_cli` is a subscription CLI whose
    reported cost is imputed, and it is the one backend that cannot be pointed at anything
    else. This is the path off it.
    """

    def _capture(self, monkeypatch, payload: dict[str, Any] | None = None):  # type: ignore[no-untyped-def]
        """Records the request the backend would have sent, and answers it."""
        import json as json_mod

        import backglass.extract.client as client_mod

        seen: dict[str, Any] = {}

        class _Response:
            def __enter__(self) -> Any:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def read(self) -> bytes:
                return json_mod.dumps(
                    payload
                    or {
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [
                                        {"function": {"arguments": json_mod.dumps({"verdict": "keep"})}}
                                    ]
                                }
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                    }
                ).encode()

        def fake_urlopen(request: Any, timeout: float = 0) -> Any:
            del timeout
            seen["url"] = request.full_url
            seen["auth"] = request.headers.get("Authorization")
            return _Response()

        monkeypatch.setattr(client_mod.urllib.request, "urlopen", fake_urlopen)
        return seen

    def test_it_points_wherever_model_base_url_says(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        seen = self._capture(monkeypatch)
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            model_backend="openai_compatible",
            model_base_url="http://127.0.0.1:11434/v1",
        )

        client.build(settings).complete(
            system="s", user="u", schema=TRIAGE_SCHEMA, model="llama3.2", budget_usd=1.0
        )

        assert seen["url"].startswith("http://127.0.0.1:11434/v1")

    def test_a_local_server_needs_no_key(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """The zero-cost, nothing-leaves-the-machine path must not be the only one that
        demands a secret. Ollama and vLLM authenticate nothing; a hosted provider on this
        same path still fails loudly at its own 401."""
        self._capture(monkeypatch)
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            model_backend="openai_compatible",
            model_base_url="http://127.0.0.1:11434/v1",
            model_api_key="",
        )
        # deepinfra with no key raises; this must not.
        client.build(settings)

    def test_an_empty_base_url_keeps_an_existing_deepinfra_config_working(self) -> None:
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            model_backend="openai_compatible",
            model_api_key="k",
        )
        built = client.build(settings)
        inner = getattr(built, "inner", built)
        assert inner.base_url == settings.deepinfra_base_url

    def test_its_spend_is_real_money_not_an_imputed_price(self) -> None:
        """The distinction the cap depends on. `claude_cli` reports a price nobody is
        charged, which is what froze extraction on 2026-08-03; a hosted endpoint bills,
        and localhost bills zero — both are true numbers, so the cap may trust them."""
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            model_backend="openai_compatible",
        )
        assert client.spend_is_imputed(settings) is False
