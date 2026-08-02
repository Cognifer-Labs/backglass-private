"""Settings, read the way they are actually read: from the environment.

Every other test constructs `Settings(...)` with real Python lists, which skips the
env-parsing path entirely. That gap hid a real bug — pydantic-settings JSON-decodes
complex types from the environment before any validator runs, so `OWNER_EMAILS=a,b`
raised a SettingsError rather than reaching the comma splitter, and the failure only
appeared when the CLI was run for the first time. `NoDecode` fixes it and these tests
are what keep it fixed.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backglass.config import Settings


def env_settings(**values: str) -> Settings:
    """Build Settings from the environment only, ignoring any developer .env."""
    import os

    previous = dict(os.environ)
    try:
        os.environ.update(values)
        return Settings(_env_file=None)  # type: ignore[call-arg]
    finally:
        os.environ.clear()
        os.environ.update(previous)


def test_comma_separated_lists_parse_from_the_environment() -> None:
    settings = env_settings(
        OWNER_EMAILS="alex.rivera@example.com, arivera@example.edu",
        GMAIL_ACCOUNTS="personal,work",
        BOUNDARY_DENY_DOMAINS="clientexample.gov, wic-partner.org",
    )
    assert settings.owner_emails == ["alex.rivera@example.com", "arivera@example.edu"]
    assert settings.gmail_accounts == ["personal", "work"]
    assert settings.boundary_deny_domains == ["clientexample.gov", "wic-partner.org"]


def test_empty_and_ragged_lists_are_tolerated() -> None:
    settings = env_settings(OWNER_EMAILS=" , a@b.com ,, ", GMAIL_ACCOUNTS="")
    assert settings.owner_emails == ["a@b.com"]
    assert settings.gmail_accounts == []


def test_both_owner_addresses_count_as_the_owner() -> None:
    """Direction (i_owe vs owed_to_me) is decided against every address here.

    The Cc fixture in tests/fixtures/commitments/07 is the case this exists for: the
    owner is reachable at the second address and must not be read as a third party.
    """
    settings = env_settings(OWNER_EMAILS="alex.rivera@example.com,arivera@example.edu")
    assert settings.owns("alex.rivera@example.com")
    assert settings.owns("Alex <arivera@example.edu>")
    assert not settings.owns("dwhitfield@example.gov")
    assert not settings.owns(None)


def test_boundary_mode_is_constrained_to_the_two_documented_options() -> None:
    """docs/08: "Pick one. There is no third option and no 'decide later.'" """
    assert env_settings(BOUNDARY_MODE="full_scope").boundary_mode == "full_scope"
    assert env_settings(BOUNDARY_MODE="exclude").boundary_mode == "exclude"
    with pytest.raises(ValidationError):
        env_settings(BOUNDARY_MODE="decide_later")


def test_model_backend_is_constrained_to_the_two_implemented_backends() -> None:
    with pytest.raises(ValidationError):
        env_settings(MODEL_BACKEND="openai")
