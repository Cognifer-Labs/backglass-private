"""Which type default an obligation gets. `plan/estimates.classify`.

Owner's ask, 2026-08-27: "continue to make better solutions at estimating time for each
activity."

There are four sources of an estimate and only one of them is a guess. `manual` is a
number the owner chose, `extracted` one the source text stated, `analyzed` one
`coursework` read off a real assignment — and `type_default` is a table lookup keyed on
what the words look like. On the live board that fallback covered 208 open commitments,
and **100 of them matched no pattern at all**, so they carried an identical 45 minutes:
46 hours of the planner's arithmetic resting on a figure nobody picked.

Every string in this file is one of the owner's own commitments. That is the method the
five student-shaped types were added by in August — count the leading verbs of what is
actually landing in `unknown`, rather than imagine what a ledger contains — and these
tests are the record of what the second pass was answering.
"""

from __future__ import annotations

import pytest

from backglass.config import Settings
from backglass.plan import estimates


@pytest.fixture
def sett(settings: Settings) -> Settings:
    return settings


# ── the clusters that were landing in `unknown` ───────────────────────────


@pytest.mark.parametrize(
    "what",
    [
        "Tell Mrs. Gathas he's back in Arizona for diploma reprint",
        "explain no-spitting/brushing instructions later",
        "communicate housing issue to ASU University Housing",
        "contact about Fall class registration",
        "let group know if he can make it",
        "answer Appa's five reflection questions",
        "reach out to Dr. Shufeldt this week",
        "keep you posted on how it goes",
        "Connect Dharsan with TJ for onboarding",
    ],
)
def test_saying_something_to_somebody_is_a_message(what: str) -> None:
    assert estimates.classify(what) == "message"


@pytest.mark.parametrize(
    "what",
    [
        "get 16 Blue Bell and 1 Fat Boy ice creams",
        "get the shower curtain",
        "reprint diploma with corrected name",
        "come and replace bedding items",
        "exchange the belt for correct size",
        "pick user up for pickleball",
        "give a ride",
    ],
)
def test_fetching_and_carrying_is_an_errand(what: str) -> None:
    assert estimates.classify(what) == "errand"


@pytest.mark.parametrize(
    "what",
    [
        "Schedule appointment with pre-med advisor",
        "schedule mandatory first-year honors advising appointment",
    ],
)
def test_booking_a_slot_is_a_call(what: str) -> None:
    """Reusing an existing type rather than minting a `schedule` one. A new type needs a
    new default, and an unmeasured default is exactly what this pass exists to reduce."""
    assert estimates.classify(what) == "call"


@pytest.mark.parametrize(
    "what",
    [
        "look for scholarships at ASU",
        "verify real deadlines for Neuroscience Scholars and Helios TGen apps",
        "find out how late court is free",
    ],
)
def test_looking_something_up_is_a_review(what: str) -> None:
    assert estimates.classify(what) == "review"


# ── and what the additions must not have taken ────────────────────────────


@pytest.mark.parametrize(
    ("what", "kind"),
    [
        # `get` is broad enough to swallow half a board. It sits at the end of the table
        # so it only ever catches what the nine patterns before it declined.
        ("Submit HW - Getting Started for CIS236", "form"),
        ("Complete Getting Familiar with Achieve (PSY101)", "form"),
        # `verify` was tried inside `review`, which is third, and rebinned this out of
        # `form`. It is a separate entry at the end for that reason.
        ("Upload ASU ID photo and verify identity", "form"),
        ("Send instructor intros from ASU address", "message"),
        ("review agreement form and accept scholarship award", "review"),
        ("Draft the personal statement", "draft"),
        ("Log hours for the week", "log"),
    ],
)
def test_the_new_patterns_stole_nothing(what: str, kind: str) -> None:
    """Measured across the owner's whole open board before and after: 37 commitments
    moved out of `unknown` and **zero** moved out of any other type. First-match-wins
    makes ordering the whole safety property, so it gets a test rather than a comment."""
    assert estimates.classify(what) == kind


@pytest.mark.parametrize(
    "what",
    ["come over", "come by for a few days over winter break"],
)
def test_a_social_visit_is_left_unknown(what: str) -> None:
    """Not everything should be classified. These are engagements — somebody visiting —
    and forcing them into `errand` at 30 minutes would be a confident wrong number where
    `unknown` is an honest vague one. The point of the pass is fewer guesses, not fewer
    unknowns."""
    assert estimates.classify(what) == "unknown"


def test_every_pattern_resolves_to_a_type_that_has_a_default(sett) -> None:  # type: ignore[no-untyped-def]
    """The alias fold's whole job. A pattern naming a type the table has no entry for
    would return an estimate of `None` and put the commitment back where it started."""
    table = estimates.defaults(sett)
    for kind, _ in estimates.TYPE_PATTERNS:
        resolved = estimates._ALIASES.get(kind, kind)
        assert resolved in table, f"{kind!r} resolves to {resolved!r}, which has no default"


# ── the feedback loop, and why it has never fired ─────────────────────────


def test_a_sample_too_small_says_so_instead_of_saying_nothing(conn) -> None:  # type: ignore[no-untyped-def]
    """docs/04 §1.3 asks for one loop: "track actuals … and let the owner adjust the type
    defaults". It returned None below the threshold, so on the owner's real ledger — 2
    finished blocks in 94 days — the loop had been silently doing nothing since it was
    written, and no surface anywhere said why.

    Still not a conclusion from a sample too small to have one. Just not silence.
    """
    report = estimates.ratio_report(conn)

    assert not report.ready
    said = report.sentence()
    assert said is not None
    assert str(estimates.RATIO_MIN_SAMPLE) in said
    assert "Done" in said, "the sentence has to name the button that fixes it"


def test_it_still_refuses_to_conclude_from_too_little(conn, sett) -> None:  # type: ignore[no-untyped-def]
    """The half that must not change. Below the threshold it may describe its own
    emptiness; it may not report a ratio, because a ratio over three items is a number
    the owner would act on and should not."""
    report = estimates.ratio_report(conn)

    said = report.sentence() or ""
    assert "Raise the type defaults" not in said
    assert "Lower the type defaults" not in said
    assert "within 5%" not in said
