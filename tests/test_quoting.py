"""Quote and signature stripping. backglass/extract/quoting.py.

One test per pattern family vendored from talon and email-reply-parser, plus the two
properties the callers depend on: a message with no quoting comes through untouched, and
cleaning is idempotent — because content_hash is computed over the cleaned body, and a
strip that is not a fixed point makes the same message hash differently on a second read.
"""

from __future__ import annotations

import pytest

from backglass.extract.quoting import clean, strip_quoted, strip_signature

GMAIL_REPLY = """Sounds good, I'll send the scope Monday.

On Fri, Jul 10, 2026 at 9:15 AM Dana Whitfield <dwhitfield@example.gov> wrote:
> Can you get me the scope this week?
> Also, the migration plan is still pending.
"""

WRAPPED_ATTRIBUTION = """Yes, Tuesday works.

On Fri, Jul 10, 2026 at 9:15 AM Dana Whitfield
<dwhitfield.with.a.very.long.address@example.gov>
wrote:
> Does Tuesday work?
"""

OUTLOOK_ORIGINAL = """I'll have the numbers to you by Thursday.

-----Original Message-----
From: Dana Whitfield
Sent: Friday, July 10, 2026 9:15 AM
Subject: Q3 numbers

Where are we on the numbers?
"""

OUTLOOK_HEADER_BLOCK = """Approved.

From: Dana Whitfield <dana@example.gov>
To: Marcus Reed
Sent: Friday, July 10, 2026 9:15 AM
Subject: Budget

Please approve the budget.
"""

FORWARDED = """Passing this along — I owe Ravi an answer by Wednesday.

---------- Forwarded message ---------
From: Ravi <ravi@example.com>

Any update?
"""

UNDERSCORE_RULE = """Confirmed for 3pm.

________________________________
From: Dana
Subject: Meeting
"""


def test_gmail_style_attribution_and_quoted_lines_are_removed() -> None:
    assert strip_quoted(GMAIL_REPLY) == "Sounds good, I'll send the scope Monday."


def test_a_wrapped_attribution_line_is_still_recognised() -> None:
    """Gmail and Apple Mail wrap "On <date>, <name> wrote:" over as many as three lines
    when the address is long. talon's six-line splitter window is why this works."""
    assert strip_quoted(WRAPPED_ATTRIBUTION) == "Yes, Tuesday works."


def test_original_message_separator_cuts_the_history() -> None:
    cleaned = strip_quoted(OUTLOOK_ORIGINAL)
    assert cleaned == "I'll have the numbers to you by Thursday."
    assert "Where are we" not in cleaned


def test_outlook_from_sent_header_block_cuts_the_history() -> None:
    cleaned = strip_quoted(OUTLOOK_HEADER_BLOCK)
    assert cleaned == "Approved."
    assert "Please approve" not in cleaned


def test_forwarded_message_separator_cuts_the_history() -> None:
    cleaned = strip_quoted(FORWARDED)
    assert cleaned.startswith("Passing this along")
    assert "Any update?" not in cleaned


def test_an_underscore_rule_cuts_the_history() -> None:
    assert strip_quoted(UNDERSCORE_RULE) == "Confirmed for 3pm."


def test_bare_quoted_lines_are_dropped_without_a_marker() -> None:
    """A forwarded body sometimes arrives with `>` prefixes and no attribution at all."""
    text = "Here is my answer.\n> the original question\n>> and one before that\n"
    assert strip_quoted(text) == "Here is my answer."


@pytest.mark.parametrize(
    "signature",
    [
        "-- \nMarcus Reed\nPrincipal, Example Co",
        "--\nMarcus Reed",
        "__________\nMarcus Reed",
        "Sent from my iPhone",
        "Sent from my Android",
        "Get Outlook for iOS",
        "Thanks,\nMarcus",
        "Best regards,\nMarcus Reed\nExample Co\n+1 602 555 0134",
        "Cheers,\nMarcus",
    ],
)
def test_signature_families_are_removed(signature: str) -> None:
    body = "The deck is due Monday."
    assert strip_signature(f"{body}\n\n{signature}\n") == body


def test_a_sign_off_mid_message_is_not_a_signature() -> None:
    """talon cuts at "Thanks," unconditionally. Here that would take a commitment with it,
    so a sign-off only opens a signature when a short name block follows."""
    text = (
        "Thanks,\n"
        "I have the contract now and I will send the countersigned copy to Dana on "
        "Thursday, along with the revised schedule and the updated budget lines.\n"
        "Let me know if anything there looks wrong before I send it.\n"
    )
    assert strip_signature(text) == text.strip()
    assert "countersigned copy" in strip_signature(text)


def test_a_message_that_is_only_a_sign_off_is_not_emptied() -> None:
    """Short is not absent. Deleting it would lose the only evidence the reply existed."""
    assert strip_signature("Thanks,\nMarcus") == "Thanks,\nMarcus"


def test_a_message_with_no_quoting_or_signature_passes_through_unchanged() -> None:
    text = "I owe Ravi the deck by Wednesday, and Dana the signed scope by Friday."
    assert strip_quoted(text) == text
    assert strip_signature(text) == text
    assert clean(text) == text


def test_clean_does_both() -> None:
    text = (
        "I'll send the scope Monday.\n\n"
        "-- \nMarcus Reed\n\n"
        "On Fri, Jul 10, 2026 at 9:15 AM Dana wrote:\n"
        "> when?\n"
    )
    assert clean(text) == "I'll send the scope Monday."


@pytest.mark.parametrize(
    "text",
    [
        GMAIL_REPLY,
        WRAPPED_ATTRIBUTION,
        OUTLOOK_ORIGINAL,
        OUTLOOK_HEADER_BLOCK,
        FORWARDED,
        UNDERSCORE_RULE,
        "Thanks,\nMarcus",
        "Plain text with no quoting at all.",
        "",
        "\n\n   \n",
    ],
)
def test_clean_is_idempotent(text: str) -> None:
    """content_hash is computed over the cleaned body (docs/07 §Gmail). A strip that is
    not a fixed point would hash the same message two different ways across runs."""
    once = clean(text)
    assert clean(once) == once
