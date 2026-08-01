"""Tier 0 rules, entity resolution, and the dedup comparison.

These are the pure functions docs/10 §Testing says should be exhaustive.
"""

from __future__ import annotations

import pytest

from backglass.config import Settings
from backglass.extract import entities, rules
from backglass.ledger import Ledger

# ─────────────────────────────────────────────────────────────────── rules


@pytest.mark.parametrize(
    ("headers", "expected_fragment"),
    [
        ({"List-Unsubscribe": "<mailto:x@y.com>"}, "bulk header"),
        ({"List-Id": "<news.example.com>"}, "bulk header"),
        ({"Precedence": "bulk"}, "precedence"),
        ({"Auto-Submitted": "auto-generated"}, "auto-submitted"),
        ({"From": "no-reply@example.com"}, "no-reply sender"),
        ({"From": "do_not_reply@example.com"}, "no-reply sender"),
        ({"From": "notifications@example.com"}, "no-reply sender"),
        ({"Content-Type": "text/calendar; method=REQUEST"}, "calendar invite"),
    ],
)
def test_rules_that_drop(headers: dict[str, str], expected_fragment: str) -> None:
    verdict = rules.classify(headers=headers, author=headers.get("From"))
    assert verdict.dropped
    assert verdict.reason is not None and expected_fragment in verdict.reason


@pytest.mark.parametrize(
    "sender",
    ["dwhitfield@example.gov", "support@example.com", "billing@vendor.com", "dana@example.com"],
)
def test_rules_never_drop_an_address_a_human_might_be_behind(sender: str) -> None:
    """triage.md: "A false positive costs one extraction call. A false negative loses a
    commitment permanently, and the user will never know it happened."

    `support@` and `billing@` are deliberately not on the no-reply list. A human on a
    support alias absolutely does make commitments.
    """
    verdict = rules.classify(headers={"From": sender}, author=sender)
    assert not verdict.dropped
    assert verdict.verdict == "unclassified"


def test_rules_never_return_keep() -> None:
    """A rule can establish that something is not worth extracting, never that it is.
    Anything it cannot classify goes to the tier-1 model."""
    verdict = rules.classify(headers={"From": "dana@example.com"}, author="dana@example.com")
    assert verdict.verdict in ("drop", "unclassified")


def test_structured_sources_drop_before_anything_else() -> None:
    """anki/avorio tallies are consumed deterministically; triaging them is pure spend."""
    verdict = rules.classify(
        headers={},
        author="anki",
        source="anki",
        structured_sources=frozenset({"anki", "avorio"}),
    )
    assert verdict.dropped
    assert verdict.reason is not None and "structured source" in verdict.reason


def test_structured_sources_match_labelled_variants_by_prefix() -> None:
    """One `calendar` entry covers `calendar:personal`, same as noise domains."""
    verdict = rules.classify(
        headers={},
        author="",
        source="calendar:personal",
        structured_sources=frozenset({"calendar"}),
    )
    assert verdict.dropped


def test_structured_sources_never_touch_other_sources() -> None:
    verdict = rules.classify(
        headers={"From": "dana@example.com"},
        author="dana@example.com",
        source="gmail:personal",
        structured_sources=frozenset({"anki", "avorio"}),
    )
    assert not verdict.dropped
    # And a prefix must be a label boundary, not a substring: "anki" != "ankiety".
    verdict = rules.classify(
        headers={}, author="", source="ankiety", structured_sources=frozenset({"anki"})
    )
    assert not verdict.dropped


def test_known_noise_domains_are_configuration() -> None:
    verdict = rules.classify(
        headers={"From": "hello@newsletter.example"},
        author="hello@newsletter.example",
        noise_senders=frozenset({"newsletter.example"}),
    )
    assert verdict.dropped
    assert verdict.reason is not None and "known-noise" in verdict.reason


# ──────────────────────────────────────────────────────────────── entities


def test_parse_counterparty() -> None:
    assert entities.parse_counterparty("Dana Whitfield <dwhitfield@example.gov>") == (
        "Dana Whitfield",
        "dwhitfield@example.gov",
    )
    assert entities.parse_counterparty("dwhitfield@example.gov") == (
        "Dwhitfield",
        "dwhitfield@example.gov",
    )
    assert entities.parse_counterparty("Dana") == ("Dana", None)


def test_the_same_person_under_two_display_names_is_one_entity(
    conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    """docs/03: "Get this wrong and the 'awaiting others' view fragments into duplicates
    and stops being useful." The address is the reliable key."""
    ledger = Ledger(conn, settings)
    first = ledger.resolve_entity("Dana Whitfield <dwhitfield@example.gov>")
    second = ledger.resolve_entity("Dana <dwhitfield@example.gov>")
    third = ledger.resolve_entity("D. Whitfield <DWhitfield@Example.GOV>")

    assert first == second == third
    assert conn.execute("SELECT COUNT(*) AS n FROM entity").fetchone()["n"] == 1


def test_resolving_the_same_entity_twice_is_not_a_write(conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(conn, settings)
    ledger.resolve_entity("Dana <dwhitfield@example.gov>")
    writes_after_create = ledger.writes
    ledger.resolve_entity("Dana <dwhitfield@example.gov>")
    assert ledger.writes == writes_after_create


def test_a_missing_counterparty_is_none_not_an_invented_entity(
    conn, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(conn, settings)
    assert ledger.resolve_entity(None) is None
    assert ledger.resolve_entity("  ") is None
    assert conn.execute("SELECT COUNT(*) AS n FROM entity").fetchone()["n"] == 0


# ─────────────────────────────────────────────────────── the dedup measure


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("revised migration plan", "the revised migration plan"),
        ("revised migration plan", "migration plan revised"),
        ("send Dana the scope", "scope sent to Dana"),
        ("Q3 budget sheet", "the Q3 budget sheet"),
    ],
)
def test_restatements_of_one_commitment_collapse(left: str, right: str) -> None:
    assert entities.similar(left, right) >= 0.85


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("revised migration plan", "vendor comparison"),
        ("Q3 budget sheet", "Q4 forecast"),
        ("send the scope", "review the contract"),
    ],
)
def test_genuinely_different_commitments_do_not_collapse(left: str, right: str) -> None:
    assert entities.similar(left, right) < 0.85


def test_the_owner_is_never_their_own_counterparty(conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    """People mail themselves reminders constantly.

    Without this guard the ledger grows an `entity` row for the owner and commitments
    they owe to themselves, which then read as "awaiting others" — the one view docs/03
    says justifies the build. The commitment is still real; it just has no counterparty.
    Both of the owner's addresses count.
    """
    ledger = Ledger(conn, settings)
    assert ledger.resolve_entity("contactdharsan@gmail.com") is None
    assert ledger.resolve_entity("K <DKesava2@ASU.EDU>") is None
    assert ledger.resolve_entity("Dana <dwhitfield@example.gov>") is not None
    assert conn.execute("SELECT COUNT(*) AS n FROM entity").fetchone()["n"] == 1


def test_noise_domains_cover_subdomains() -> None:
    """One `example.com` entry covers `mail.example.com`, the same way the boundary reads
    a domain entry. Every entry here is a model call never made."""
    noise = frozenset({"newsletter.example"})
    for sender in ("hello@newsletter.example", "bounce@mail.newsletter.example"):
        verdict = rules.classify(headers={"From": sender}, author=sender, noise_senders=noise)
        assert verdict.dropped, sender
    safe = "dana@notnewsletter.example"
    assert not rules.classify(headers={"From": safe}, author=safe, noise_senders=noise).dropped
