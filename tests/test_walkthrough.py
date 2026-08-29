"""The exact assignment link, and the steps under it.

Two of these are regression tests for things the live ledger was already getting wrong.
Every one of its 223 Canvas rows carried a URL to the *calendar* rather than to the
assignment, so "open the assignment" was one click short for two months. And the first
walkthrough built offered "how to start it · 3 steps" on a PSY 101 row whose three steps
were open-it, budget-it and hand-it-in — scaffolding every assignment gets, and nothing
the row above the fold had not already said.
"""

from __future__ import annotations

from datetime import datetime

from backglass import walkthrough

#: The shape the ICS feed publishes, verbatim from the ledger.
CALENDAR_URL = (
    "https://canvas.asu.edu/calendar?include_contexts=course_274090&month=08&year=2026"
    "#assignment_7833006"
)


def test_the_link_goes_to_the_assignment_and_not_to_the_calendar() -> None:
    assert (
        walkthrough.canvas_link(CALENDAR_URL, "assignment:7833006")
        == "https://canvas.asu.edu/courses/274090/assignments/7833006"
    )


def test_a_link_it_cannot_build_falls_back_to_the_one_the_feed_gave() -> None:
    """A deep link built from half the ids is a 404 that reads as the ledger being wrong
    about the assignment rather than about the URL."""
    assert walkthrough.canvas_link(CALENDAR_URL, "") == CALENDAR_URL
    assert walkthrough.canvas_link("https://canvas.asu.edu/x", "assignment:1") == (
        "https://canvas.asu.edu/x"
    )
    assert walkthrough.canvas_link("", "assignment:1") == ""


def _build(**kwargs: object) -> walkthrough.Walkthrough:
    base: dict[str, object] = {
        "title": "T - Dataset Evaluation",
        "description": "",
        "kind": "assignment",
        "url": CALENDAR_URL,
        "external_id": "assignment:7833006",
        "points": 60.0,
        "minutes": 172,
        "effort_quote": "",
        "materials": [],
    }
    base.update(kwargs)
    return walkthrough.build(**base)  # type: ignore[arg-type]


def test_work_that_is_just_answering_questions_gets_the_link_and_says_why() -> None:
    """The owner's own boundary, 2026-08-29: a walkthrough "if it isnt just answering
    questions"."""
    for kind in ("quiz", "exam", "learningcurve", "form"):
        result = _build(kind=kind, description="1. do a thing\n2. do another")
        assert result.offered is False
        assert kind in result.reason
        assert result.link.endswith("/assignments/7833006")


def test_a_video_whose_instruction_is_to_answer_what_appears_is_the_same_case() -> None:
    """Every CIS 236 lecture row reads this way and is classified `video`, not `quiz`."""
    result = _build(
        kind="video",
        description="Watch the video and answer any questions that appear.",
    )
    assert result.offered is False
    assert "answer" in result.reason


def test_scaffolding_alone_is_not_a_walkthrough() -> None:
    """Open it, budget it, hand it in — three steps that say nothing the row did not.
    The live render offered exactly this on a PSY 101 assignment."""
    result = _build(description="")
    assert result.offered is False
    assert "nothing to walk through" in result.reason


def test_the_steps_are_the_ones_the_course_itself_wrote() -> None:
    result = _build(
        description=(
            "Read the brief.\n"
            "1. Download the provided dataset.\n"
            "2. Build the chart in Excel.\n"
            "“Time is money – make it count.”\n"
            "- Submit as a PDF.\n"
        ),
        materials=[("software", "Microsoft Excel", "use Excel for the chart")],
    )
    assert result.offered is True
    said = [step.text for step in result.steps]
    assert "Download the provided dataset." in said
    assert "Build the chart in Excel." in said
    assert "Submit as a PDF." in said
    # The scenario talking is not an instruction, and a checklist that mixes the two is a
    # checklist nobody finishes.
    assert not any(text.startswith("“Time is money") for text in said)
    assert "Have Microsoft Excel open before you start" in said
    # Every step carries the words behind it, or it is scaffolding this module wrote.
    for step in result.steps:
        if step.lane == "do":
            assert step.quote, f"unsourced step: {step.text}"


def test_a_long_brief_is_capped_and_says_what_it_cut() -> None:
    """CIS 236's final brief flattens to 70+ bulleted lines. A row that opens into
    seventy checkboxes has moved the wall, not removed it — and a cap that says nothing
    about itself is the silent truncation this project refuses."""
    lines = "\n".join(f"{n}. step number {n}" for n in range(1, 41))
    result = _build(description=lines)
    written = [step for step in result.steps if step.lane == "do"]
    assert len(written) == walkthrough.MAX_COURSE_STEPS
    remainder = [step for step in result.steps if "more instructions" in step.text]
    assert len(remainder) == 1
    assert "25 more" in remainder[0].text
    assert remainder[0].href.endswith("/assignments/7833006")


def test_the_lock_date_and_the_estimate_ride_along() -> None:
    result = _build(
        description="1. Do the thing.",
        minutes=172,
        effort_quote="about three hours",
        lock_at=datetime(2026, 9, 14, 23, 59),
    )
    said = [step.text for step in result.steps]
    assert "Give it about 2h52" in said
    assert any("closes it 14 sep" in text.lower() for text in said)
    assert said[0].startswith("Open the assignment on Canvas — 60 points")
