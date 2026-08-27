"""The AI-tell sweep, and the guarantee that no shipped template carries one.

Two halves, and the second is the one that matters most in a year. Testing `clean` and
`findings` proves the sweep works; asserting that `reachout.TEMPLATES` is sweep-clean
proves it is *used*, and catches the next template someone writes with an em dash in it.
That regression is not hypothetical — all three templates carried one until 2026-08-25,
and the `ask` template opened "I hope things are going well" for its whole life.
"""

from __future__ import annotations

import pytest

from backglass.draft import sweep
from backglass.people.reachout import TEMPLATES


# --- clean: characters, and only characters ------------------------------------------


def test_em_and_en_dashes_become_spaced_hyphens():
    assert sweep.clean("time — I mainly") == "time - I mainly"
    assert sweep.clean("time–I mainly") == "time - I mainly"


def test_digit_ranges_keep_a_bare_hyphen():
    """A year range is joined, not a sentence break, so it must not gain spaces."""
    assert sweep.clean("2019–2024 was busy") == "2019-2024 was busy"
    assert sweep.clean("pages 10—12") == "pages 10-12"


def test_curly_quotes_and_apostrophes_go_straight():
    assert sweep.clean("I’d say “yes”") == "I'd say \"yes\""


def test_ellipsis_glyph_expands():
    assert sweep.clean("wait…") == "wait..."


def test_invisible_characters_are_removed():
    assert sweep.clean("clean​text﻿") == "cleantext"


def test_non_breaking_space_becomes_a_space():
    assert sweep.clean("a b") == "a b"


def test_markdown_bold_is_stripped():
    assert sweep.clean("**Kind regards**") == "Kind regards"


def test_clean_never_touches_phrasing():
    """The restraint that lets `clean` run unattended: it changes look, never words."""
    text = "I hope this email finds you well. I'd be happy to help."
    assert sweep.clean(text) == text


def test_clean_is_idempotent():
    text = "Hi — I’d say “2019–2024”…"
    once = sweep.clean(text)
    assert sweep.clean(once) == once


def test_clean_survives_empty_input():
    assert sweep.clean("") == ""
    assert sweep.clean(None) == ""  # type: ignore[arg-type]


# --- findings: report, change nothing -------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "I hope this email finds you well.",
        "I hope this message finds you well.",
        "Just circling back on this.",
        "Just following up!",
        "I'd be happy to take a look.",
        "Please don't hesitate to ask.",
        "Feel free to reach out.",
        "I look forward to hearing from you.",
        "I wanted to reach out about the role.",
        "As an AI language model, I cannot.",
    ],
)
def test_known_phrases_are_found(text):
    assert any(f.category == "phrase" for f in sweep.findings(text)), text


def test_curly_apostrophe_variants_of_a_phrase_are_found():
    assert any(f.category == "phrase" for f in sweep.findings("I’d be happy to."))


def test_placeholders_are_found_and_block_sending():
    text = "Best,\n[Your Name]"
    assert any(f.category == "placeholder" for f in sweep.findings(text))
    assert not sweep.is_sendable(text)


def test_a_merely_inflated_draft_is_still_sendable():
    """Taste is the owner's call; an unfilled slot is a bug. Only the bug blocks."""
    text = "This was a crucial and robust conversation."
    assert any(f.category == "inflated" for f in sweep.findings(text))
    assert sweep.is_sendable(text)


def test_emoji_is_reported_but_never_removed():
    text = "Thanks 👍"
    assert any(f.category == "emoji" for f in sweep.findings(text))
    assert "👍" in sweep.clean(text)


def test_ordinary_punctuation_is_not_mistaken_for_emoji():
    assert not any(f.category == "emoji" for f in sweep.findings("50% → 60% costs $4"))


def test_findings_are_ordered_by_position():
    offsets = [f.offset for f in sweep.findings("I’d be happy to — [Your Name] 👍")]
    assert offsets == sorted(offsets)


def test_findings_quote_the_matched_text_verbatim():
    (found,) = [f for f in sweep.findings("Feel free to ask") if f.category == "phrase"]
    assert found.text == "Feel free to"


def test_a_clean_draft_reports_nothing():
    text = "Good afternoon Todd,\n\nYes, please do. I'll reach out this week.\n\nBest,\nDharsan"
    assert sweep.findings(text) == ()
    assert sweep.is_sendable(text)


# --- report: what the owner is about to send ------------------------------------------


def test_report_describes_the_cleaned_text_not_the_original():
    """What clean already fixed must not appear in the list of things left to decide."""
    cleaned, found = sweep.report("Hi — I’d be happy to help")
    assert "—" not in cleaned
    assert not any(f.category == "watermark" for f in found)
    assert any(f.category == "phrase" for f in found)


# --- the guarantee ---------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_every_reachout_template_is_sweep_clean(name):
    """No template ships a tell. This is the test that catches the next one written."""
    spec = TEMPLATES[name]
    text = spec.subject + "\n" + "\n".join(spec.paragraphs)
    found = sweep.findings(text)
    assert not found, "\n".join(f.line() for f in found)


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_template_slots_are_not_mistaken_for_placeholders(name):
    """`{first}` is a slot the renderer fills; `[Your Name]` is one nobody will."""
    spec = TEMPLATES[name]
    text = spec.subject + "\n" + "\n".join(spec.paragraphs)
    assert sweep.is_sendable(text)
