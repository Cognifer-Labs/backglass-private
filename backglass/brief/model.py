"""The shape of a brief, and the enforcement of its hard requirements.

docs/05 §Hard requirements is a table of seven. Four of them are structural and live here
rather than in the renderer, because a requirement you can violate by writing a new
renderer is not a requirement:

  B1  under 400 words, enforced in code, lowest-priority section truncated and said so
  B2  every line links to its source; a line with no provenance does not render
  B3  empty sections are omitted, not shown empty
  B4  no item appears in two sections; precedence is the section order in docs/05

B2 is the one that decides whether this product is worth having. CLAUDE.md: "Trust
collapses after two unsourced wrong claims and never comes back." So `Line` cannot be
constructed without provenance — not "should not", cannot. There is no default, no
`None`, and `Brief.render_lines()` raises rather than dropping a bad line quietly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

WORD_LIMIT = 400
_WORDS = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’\-/:.]*")


class MissingProvenance(RuntimeError):
    """A line tried to make a claim with nothing behind it. B2. Never caught."""


class Provenance(Protocol):
    """Something a claim can point at."""

    @property
    def label(self) -> str:
        """Short human text, e.g. 'Gmail · 14 Jul'."""

    def url(self, base: str) -> str:
        """Where the reader lands when they check."""


@dataclass(frozen=True)
class SourceRef:
    """A `source_item` — the evidence half of "documents as evidence"."""

    source: str
    external_id: str
    occurred_at: str
    title: str | None = None
    #: The `source_item` rowid, which is what `/source/{id}` is keyed by. `external_id`
    #: cannot stand in for it: it is only unique *within* a source, so two connectors can
    #: legitimately hand back the same string.
    source_item_id: int | None = None

    @property
    def label(self) -> str:
        kind = self.source.split(":", 1)[0]
        return f"{kind} · {self.occurred_at[:10]}"

    def url(self, base: str) -> str:
        """Deep-link into the originating account where one exists.

        Gmail's `#all/<id>` form opens the message regardless of which label it lives
        under, which matters because the connector does not track labels.

        Everything else lands on the local source page. This used to be
        `/source/{external_id}`, a route that has never existed — and since the ledger
        holds no Gmail at all until the owner authenticates it, that meant every
        provenance link in the brief was a 404. B2 says a line with no provenance does
        not render; a line whose provenance link is dead passes that check and fails the
        reader, which is worse.
        """
        if self.source.startswith("gmail"):
            return f"https://mail.google.com/mail/u/0/#all/{self.external_id}"
        if self.source_item_id is not None:
            return f"{base.rstrip('/')}/source/{self.source_item_id}"
        return f"{base.rstrip('/')}/#panel-sources"


@dataclass(frozen=True)
class LedgerRef:
    """Derived state — a day plan, a credential, a run.

    Still provenance: the claim "6h 15m available" is not unsourced, it is sourced to a
    row the reader can open. It just has no external URL, so it points at the dashboard.
    """

    table: str
    row_id: str
    described: str

    @property
    def label(self) -> str:
        return self.described

    def url(self, base: str) -> str:
        """The dashboard surface that shows this row.

        `/{table}/{row_id}` read like a REST resource and routed to nothing: `/plans/…`,
        `/goals/12`, `/commitments/21` and `/checklist/3` are not pages (the last two are
        POST-only write endpoints, so clicking one got a 405 rather than even an honest
        404). The map below points at surfaces that exist, and
        tests/test_provenance.py walks every URL the brief generates against the app's
        real route table so a future rename cannot quietly restore the 404s.
        """
        root = base.rstrip("/")
        if self.table == "plans":
            return f"{root}/schedule?date={self.row_id}"
        if self.table == "goals":
            return f"{root}/goals"
        if self.table in ("sources", "runs"):
            # Both land on the Sources panel: it is the surface that states when each
            # source last ran and what it said. A dedicated /runs page is worth building
            # (the `run` table is rendered nowhere), and this points at the panel that
            # answers the same question today rather than at the page that does not
            # exist yet.
            return f"{root}/#panel-sources"
        if self.table == "checklist":
            return f"{root}/#panel-checklist"
        if self.table == "commitments":
            return f"{root}/#panel-board"
        return f"{root}/"


@dataclass(frozen=True)
class Line:
    """One claim. B2: provenance is a required positional argument, deliberately."""

    text: str
    provenance: Provenance
    #: Drives the ink in design-system.md §4. None means neutral, no fill.
    status: str | None = None
    #: Set when the line is about a commitment, so B4 can deduplicate across sections.
    commitment_id: int | None = None

    def __post_init__(self) -> None:
        if not self.text or not self.text.strip():
            raise ValueError("a brief line cannot be empty")
        if self.provenance is None:
            raise MissingProvenance(f"line has no provenance: {self.text!r}")


@dataclass(frozen=True)
class Note:
    """A statement about the brief itself, not a claim about the world.

    "2 items in Needs review omitted for length" is not sourced to anything because it
    asserts nothing about the ledger. Kept as a separate type so it cannot be used as a
    back door around B2 — a Note may never carry a commitment.
    """

    text: str


@dataclass
class Section:
    """One of the eight in docs/05 §Sections, in order."""

    #: 1–8, the precedence order in docs/05. Lower wins on B4, and B1 truncates from the
    #: highest number down.
    priority: int
    title: str
    lines: list[Line] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.lines and not self.notes


@dataclass
class Brief:
    generated_for_date: str
    kind: str = "daily"
    sections: list[Section] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)

    # ── B3, B4, B1, in that order ────────────────────────────────────────

    def add(self, section: Section) -> None:
        """B3. An empty section is not added at all, so it cannot render as a header."""
        if not section.empty:
            self.sections.append(section)

    def deduplicate(self) -> int:
        """B4. No item appears in two sections; precedence is the docs/05 order.

        A commitment that is both slipping and low-confidence belongs in Slipping, because
        Slipping is section 4 and Needs review is section 8. Returns how many lines were
        removed, so the caller can assert on it.
        """
        seen: set[int] = set()
        removed = 0
        for section in sorted(self.sections, key=lambda s: s.priority):
            kept: list[Line] = []
            for line in section.lines:
                if line.commitment_id is not None:
                    if line.commitment_id in seen:
                        removed += 1
                        continue
                    seen.add(line.commitment_id)
                kept.append(line)
            section.lines = kept
        self.sections = [s for s in self.sections if not s.empty]
        return removed

    def word_count(self) -> int:
        return len(_WORDS.findall(self.as_text()))

    def enforce_word_limit(self, limit: int = WORD_LIMIT) -> list[str]:
        """B1. "Enforced in code; truncate the lowest-priority section and say so."

        Whole sections go, not individual lines. A half-rendered Awaiting section reads as
        "these are all the things people owe you", which is a worse failure than an absent
        section that says why it is absent.

        The last remaining section is never dropped: a brief truncated to nothing is
        indistinguishable from the silence docs/05 B6 exists to prevent.
        """
        dropped: list[str] = []
        while self.word_count() > limit and len(self.sections) > 1:
            victim = max(self.sections, key=lambda s: s.priority)
            self.sections.remove(victim)
            count = len(victim.lines)
            dropped.append(victim.title)
            self.notes.append(
                Note(
                    f"{victim.title} omitted for length — "
                    f"{count} item{'s' if count != 1 else ''}. See the dashboard."
                )
            )

        # Last resort: one section can exceed the ceiling on its own, and B1 is an
        # absolute limit rather than a target — "under 400 words", not "under 400 words
        # unless that is inconvenient". Trim from the end of the last section, which every
        # builder orders least-urgent-last, and say how many went. The note is what keeps
        # this from reading as a complete list, which was the objection to line-level
        # truncation in the first place.
        if self.word_count() > limit and self.sections:
            section = self.sections[0]
            trimmed = 0
            while self.word_count() > limit and len(section.lines) > 1:
                section.lines.pop()
                trimmed += 1
            if trimmed:
                dropped.append(f"{section.title} (partial)")
                section.notes.append(
                    Note(f"{trimmed} more in {section.title}, omitted for length.")
                )
        return dropped

    # ── rendering support ────────────────────────────────────────────────

    def ordered(self) -> list[Section]:
        return sorted(self.sections, key=lambda s: s.priority)

    def all_lines(self) -> list[Line]:
        return [line for section in self.ordered() for line in section.lines]

    def assert_provenance(self) -> None:
        """B2, checked once more immediately before render.

        `Line.__post_init__` already makes an unsourced line unconstructable. This is the
        belt to that pair of braces, and it is a hard failure rather than a warning
        because PROMPT.md §Session 4 says so: "A line without provenance does not render —
        make that a hard failure in the renderer, not a warning."
        """
        for line in self.all_lines():
            if line.provenance is None:
                raise MissingProvenance(f"line has no provenance: {line.text!r}")

    def as_text(self) -> str:
        """The plaintext alternative.

        docs/10 §Email delivery: generated "from the same data rather than by stripping
        tags". This is also what the word count is measured against, so the ceiling
        applies to what a person reads and not to markup.
        """
        out: list[str] = []
        for section in self.ordered():
            out.append(section.title.upper())
            out.extend(f"  {line.text}  [{line.provenance.label}]" for line in section.lines)
            out.extend(f"  ({note.text})" for note in section.notes)
            out.append("")
        out.extend(f"({note.text})" for note in self.notes)
        return "\n".join(out).strip()
