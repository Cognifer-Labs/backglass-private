"""The exact assignment, and how to start it.

Owner's ask, 2026-08-29: *"each thing should have a hyperlink to the exact assignment
with a walkthrough if it isnt just answering questions."*

Two halves, and the first one turned out to be a bug rather than a feature.

**The link.** Every one of the ledger's 223 Canvas rows carries a `url` that goes to the
*calendar*, not to the assignment: `…/calendar?include_contexts=course_274090&month=08#
assignment_7833006`. That is what the ICS feed publishes, and following it lands the owner
on a month view with an anchor Canvas does not always honour — one click short of the page
the work is actually done on, every time, for two months. Both ids needed to fix it were
already in the row: the course in the query string, the assignment in `external_id`
(`assignment:7833006`). `canvas_link` puts them together into the page Canvas itself links
that anchor to. Nothing is fetched and nothing is guessed; if either id is missing the
feed's own URL is returned unchanged, because a wrong deep link is worse than a shallow
right one.

**The walkthrough, and what it is not.** It is not a model writing advice about coursework
it has not read. Rule 1 — every generated claim links to its source — does not get an
exception because the claim is useful, and an invented step ("draft an outline first") is
exactly the unsourced confident sentence that costs the owner their trust in the page. So
every step here is something the assignment, the ledger or the course itself already said,
carried with the words it was read from:

* the direct link, and the points and the lock date Canvas published beside it
* the software the description names — `TOOL_LEXICON` already finds it, and "this exam
  needs a browser you have not installed" is worth more the evening before than any
  estimate
* the readings and links it names, each with its own href
* **the course's own instructions**, when the description contains a step list — pulled
  out verbatim rather than paraphrased
* the estimate, and the sentence the number was read from

**"If it isn't just answering questions"** is the owner's own boundary and it is enforced
here. A quiz, an exam, a LearningCurve drill and an administrative form are answered, not
worked through; so is a CIS 236 video whose whole description is "Watch the video and
answer any questions that appear". Those get the link and no walkthrough, and the page
says which of the two it is rather than showing an empty fold — an assignment that offers
a walkthrough and then has nothing to say is worse than one that never offered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

#: Canvas's own deep-link shape: `…/courses/<course>/assignments/<assignment>`. It is the
#: page the calendar anchor resolves to, and the one the owner's own open tabs use.
_CANVAS_ASSIGNMENT = "{host}/courses/{course}/assignments/{assignment}"

#: `course_274090` out of the feed's calendar URL.
_COURSE_ID = re.compile(r"\bcourse_(\d+)\b")

#: `https://canvas.asu.edu` out of it, so an institution that is not ASU still works.
_HOST = re.compile(r"^(https?://[^/]+)")

#: `assignment:7833006` — the feed's own external id for the row.
_EXTERNAL = re.compile(r"^assignment:(\d+)$")

#: What is answered rather than worked through. The owner's boundary, as a set: a quiz, an
#: exam, an adaptive drill and an administrative form are all "just answering questions",
#: and a walkthrough of one would be four lines telling the owner to open a quiz.
ANSWERING_ONLY = frozenset({"quiz", "exam", "learningcurve", "form"})

#: The same boundary where the *description* says it rather than the type. Every CIS 236
#: lecture video in the ledger reads "Watch the video and answer any questions that
#: appear" — 152 characters, no steps, nothing to walk through, and classified `video`
#: rather than `quiz` because it is one.
_ANSWER_ONLY_TEXT = re.compile(
    r"answer any questions that appear|answer the questions? that appear", re.I
)

#: A line the course wrote as a step. Numbered, lettered or bulleted — the three ways a
#: Canvas description spells a list once its HTML has been flattened to text.
_STEP_LINE = re.compile(r"^\s*(?:\d{1,2}[.)]|[a-h][.)]|[-*•·–])\s+(?P<text>\S.*)$")

#: A bulleted line that is a quotation is the scenario talking, not an instruction. CIS
#: 236's briefs bullet their fictional manager's remarks alongside the requirements —
#: *"You've earned this seat at the table – now make it count."* is worth reading and is
#: not a step, and a checklist that mixes the two is a checklist nobody finishes.
_QUOTED = re.compile(r'^["“”\'‘’]')

#: How many of the course's own steps the fold carries before it stops and says so. A CIS
#: 236 final brief flattens to 70+ bulleted lines; a to-do row that opens into seventy
#: checkboxes has moved the wall rather than removed it. The remainder is named and
#: linked, never dropped silently.
MAX_COURSE_STEPS = 15

#: `[WeVideo User Guide] (http://links.asu.edu/WeVideo-Learner)` — the shape
#: `canvas_ics` leaves a link in, keeping the URL the API's HTML hides behind anchor text.
_LINK = re.compile(r"\[([^\]]{1,120})\]\s*\((https?://[^\s)]+)\)")


@dataclass(frozen=True)
class Step:
    """One thing to do, and the words behind it.

    `quote` is not decoration. A step with no quote is a step this module invented, and
    the template prints the quote on hover for exactly that reason — the owner can see
    which sentence of their own coursework produced the line they are reading.
    """

    text: str
    quote: str = ""
    href: str = ""
    #: `open`, `have`, `read`, `do`, `budget`, `hand in` — the glyph column, so the list
    #: is never colour or order alone (§8 rule 3).
    lane: str = "do"


@dataclass(frozen=True)
class Walkthrough:
    """The steps, or the reason there are none."""

    link: str
    kind: str
    steps: list[Step] = field(default_factory=list)
    #: Why this one has no walkthrough, in the owner's terms. Printed instead of an empty
    #: fold: "nothing here" and "nothing to say about this kind of thing" are different
    #: facts and only one of them is a gap.
    reason: str = ""

    @property
    def offered(self) -> bool:
        return bool(self.steps)


def canvas_link(url: str, external_id: str) -> str:
    """The assignment's own Canvas page, out of the calendar URL the feed published.

    Falls back to the feed's URL whenever either id is missing — a deep link built from
    half the information would be a 404 that looks like the ledger being wrong about the
    assignment rather than about the link.
    """
    course = _COURSE_ID.search(url or "")
    host = _HOST.match(url or "")
    assignment = _EXTERNAL.match((external_id or "").strip())
    if not (course and host and assignment):
        return url or ""
    return _CANVAS_ASSIGNMENT.format(
        host=host.group(1), course=course.group(1), assignment=assignment.group(1)
    )


def _clean(text: str) -> str:
    """The description as prose: links unwrapped to their labels, whitespace collapsed."""
    return " ".join(_LINK.sub(r"\1", text or "").split())


def _quote(text: str, width: int = 160) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def course_steps(description: str) -> list[Step]:
    """The step list the course itself wrote, verbatim, or nothing.

    Verbatim is the whole point. A summarised instruction is a claim about an instruction,
    and the owner is graded against the original — so the line is carried across as it was
    written and the description it came from is the quote behind it.
    """
    steps: list[Step] = []
    for raw in (description or "").replace("\r", "").split("\n"):
        found = _STEP_LINE.match(raw)
        if found is None:
            continue
        text = _clean(found.group("text"))
        if len(text) < 4 or _QUOTED.match(text):
            continue
        link = _LINK.search(found.group("text"))
        steps.append(
            Step(text=text, quote=_quote(raw), href=link.group(2) if link else "", lane="do")
        )
    return steps


def build(
    *,
    title: str,
    description: str,
    kind: str,
    url: str,
    external_id: str,
    points: float | None = None,
    minutes: int | None = None,
    effort_quote: str = "",
    materials: list[tuple[str, str, str]] | None = None,
    lock_at: datetime | None = None,
) -> Walkthrough:
    """Everything the ledger can say about how to start this piece of work.

    `materials` is `(kind, name, detail)` as `assignment_material` stores it — `detail`
    carries the URL on a `link` row, which is why the readings in this list are clickable
    and the software is not.
    """
    link = canvas_link(url, external_id)
    text = description or ""

    if kind in ANSWERING_ONLY:
        return Walkthrough(
            link=link,
            kind=kind,
            reason=f"a {kind} is answered, not worked through — the link is the whole of it",
        )
    if _ANSWER_ONLY_TEXT.search(text):
        return Walkthrough(
            link=link,
            kind=kind,
            reason="the assignment's own instruction is to watch it and answer what "
            "appears, so there is nothing to walk through",
        )

    steps: list[Step] = []
    if link:
        opening = "Open the assignment on Canvas"
        if points:
            opening += f" — {points:g} point{'' if points == 1 else 's'}"
        steps.append(Step(text=opening, href=link, quote=_quote(title), lane="open"))

    for material_kind, name, detail in materials or []:
        if material_kind == "software":
            steps.append(
                Step(
                    text=f"Have {name} open before you start",
                    quote=detail or "",
                    lane="have",
                )
            )
    for material_kind, name, detail in materials or []:
        if material_kind in ("reading", "document"):
            where = f" — {detail}" if detail else ""
            steps.append(Step(text=f"Read {name}{where}", quote=detail or "", lane="read"))
        elif material_kind == "link":
            steps.append(Step(text=name, href=detail, quote=detail or "", lane="read"))

    written = course_steps(text)
    steps.extend(written[:MAX_COURSE_STEPS])
    if len(written) > MAX_COURSE_STEPS:
        # The cap, printed with what it cut. A truncation that says nothing about itself
        # is the silent drop this project refuses — and the rest is one click away on the
        # page the first step already opens.
        steps.append(
            Step(
                text=f"…and {len(written) - MAX_COURSE_STEPS} more instructions on the "
                "Canvas page — read them there",
                href=link,
                lane="read",
            )
        )

    if minutes:
        hours, rest = divmod(int(minutes), 60)
        if hours and rest:
            spell = f"{hours}h{rest:02d}"
        else:
            spell = f"{hours}h" if hours else f"{rest}m"
        steps.append(
            Step(
                text=f"Give it about {spell}",
                quote=_quote(effort_quote) if effort_quote else "",
                lane="budget",
            )
        )
    if lock_at is not None:
        steps.append(
            Step(
                text=f"Canvas closes it {lock_at.strftime('%-d %b at %-I:%M%p').lower()}",
                lane="budget",
            )
        )
    if link:
        steps.append(Step(text="Hand it in on the same page", href=link, lane="hand in"))

    # The test is content, not length. Open-it, budget-it and hand-it-in are scaffolding
    # every assignment gets and none of them tells the owner anything the row above the
    # fold did not — the first live render offered "how to start it · 3 steps" on a PSY
    # 101 assignment and all three were scaffolding. A walkthrough is worth opening when
    # the course named something: a tool to have ready, a reading to do first, or a step
    # of its own.
    if not any(step.lane in ("have", "read", "do") for step in steps):
        return Walkthrough(
            link=link,
            kind=kind,
            reason="its description names no tools, readings or steps, so there is "
            "nothing to walk through that the link does not already show",
        )
    return Walkthrough(link=link, kind=kind, steps=steps)
