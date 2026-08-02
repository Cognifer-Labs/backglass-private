"""The correction feedback loop, read back.

web/actions.py has said since it was written that "the distribution over fifty
rejections says which part of the extraction prompt is wrong" — and then nothing read
the distribution. The owner clears the review queue every morning, producing labelled
training signal with a typed reason on every reject and the model's own score preserved
on every accept, and until this module existed all of it went into a text column and
stayed there. This is the read side, and only the read side: nothing here writes.

Three questions, in the order they change what you do next:

1. **Which reason dominates.** Rejections are typed for exactly this — free text would
   not aggregate. A pile that is two-thirds `wrong_date` is a date-resolution bug, not a
   prompt-tone problem, and points at one file.

2. **Whether the model's confidence means anything.** The review queue exists because
   `confidence_threshold` splits "fact" from "ask the owner". That split is only worth
   having if the score it thresholds is calibrated: if the owner rejects half of what the
   model scores 0.9, the threshold is decorative and every number downstream of it is
   too. The calibration table is the one number in this report that can tell the owner to
   move the threshold.

3. **Where the bad extractions come from.** Per-source and per-sender rejection rates say
   whether the problem is the prompt (spread evenly) or one pathological correspondent
   (concentrated), which are different fixes.

The honesty rule this report is built around: **it refuses to draw conclusions from a
sample too small to have any.** A three-row distribution rendered as a confident table is
worse than no table, because the owner will act on it. Below `MEANINGFUL_MINIMUM` the
report says how far off it is and stops. That threshold is docs/11's own "after fifty
rejections", taken literally.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from backglass.db import query
from backglass.ledger import USER_ID
from backglass.web.actions import REJECT_REASONS

#: docs/11 §4, taken at its word: "After fifty rejections, the distribution tells you
#: which part of the extraction prompt to fix." Below this the report reports its own
#: emptiness instead of a distribution. Counted over all corrections rather than
#: rejections alone, because the calibration table needs the accepts too.
MEANINGFUL_MINIMUM = 50

#: The dominant reject reason, mapped to the thing to actually go and edit. This is the
#: whole point of typing the reasons: each category was chosen to be a different failure
#: with a different home, so a distribution is a work queue rather than a mood ring.
REASON_REMEDY = {
    "wrong_date": (
        "backglass/extract/dates.py, and the date section of "
        "specs/extraction-prompts/extract-commitments.md"
    ),
    "not_a_commitment": (
        "specs/extraction-prompts/triage.md — these should not have survived tier 1"
    ),
    "not_mine": (
        "the direction/ownership rules in specs/extraction-prompts/extract-commitments.md"
    ),
    "already_done": (
        "nothing in the prompt: the model cannot see a completion it was never shown. "
        "A pile of these means the queue is being cleared too slowly, not extracted wrong"
    ),
    # Not one of the four buttons today — dedup runs before anything reaches the queue,
    # so a survivor is a dedup miss the owner currently has no word for and rejects as
    # something else. Mapped anyway, so that the day the vocabulary grows a fifth button
    # the report already knows where duplicates come from.
    "duplicate": (
        "the dedup threshold (settings.dedup_threshold) in backglass/extract/commitments.py"
    ),
}


def reason_label(reason: str) -> str:
    """The words on the button the owner clicked; unknown categories print raw."""
    return REJECT_REASONS.get(reason, reason)

#: The two shapes web/actions.py writes, and the only two this module understands. Kept
#: as patterns next to each other so that a drift in either one is a one-line fix here
#: and a loudly failing test in tests/test_corrections.py, which builds its fixtures by
#: calling actions.accept/actions.reject rather than by writing these strings by hand.
_REJECTED = re.compile(r"^rejected:(?P<reason>[a-z_]+)$")
_ACCEPTED = re.compile(r"^accepted by owner \(model said (?P<confidence>[0-9.]+)\)$")


@dataclass(frozen=True)
class Correction:
    """One owner verdict, with the model's original score attached."""

    commitment_id: int
    verdict: str  # accept | reject
    reason: str | None  # reject only
    confidence: float | None  # what the MODEL said, never the post-accept 1.0
    source: str
    author: str | None
    extraction_version: str | None
    corrected_at: str


@dataclass(frozen=True)
class Bucket:
    """One tenth of the confidence range, and how the owner ruled inside it."""

    low: float
    accepted: int
    rejected: int

    @property
    def total(self) -> int:
        return self.accepted + self.rejected

    @property
    def accept_rate(self) -> float:
        return self.accepted / self.total if self.total else 0.0


@dataclass(frozen=True)
class SourceStat:
    """Rejection rate for one source or one sender.

    The denominator is corrections, NOT extractions. Only items below
    `confidence_threshold` ever reach the review queue, so a high-confidence extraction
    is never accepted or rejected at all — dividing by every extraction would bury the
    signal under items the owner was never asked about. What this rate answers is: "of
    the things this sender made the model unsure about, how many were junk."
    """

    key: str
    rejected: int
    total: int

    @property
    def reject_rate(self) -> float:
        return self.rejected / self.total if self.total else 0.0


@dataclass(frozen=True)
class Report:
    days: int
    corrections: list[Correction]
    reasons: dict[str, int] = field(default_factory=dict)
    reasons_by_source: dict[str, dict[str, int]] = field(default_factory=dict)
    reasons_by_version: dict[str, dict[str, int]] = field(default_factory=dict)
    buckets: list[Bucket] = field(default_factory=list)
    worst_sources: list[SourceStat] = field(default_factory=list)
    worst_senders: list[SourceStat] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.corrections)

    @property
    def accepts(self) -> int:
        return sum(1 for c in self.corrections if c.verdict == "accept")

    @property
    def rejects(self) -> int:
        return sum(1 for c in self.corrections if c.verdict == "reject")

    @property
    def meaningful(self) -> bool:
        """Whether the sample is large enough to read anything off."""
        return self.total >= MEANINGFUL_MINIMUM

    @property
    def dominant_reason(self) -> str | None:
        """The most-rejected category, or None on a tie or an empty pile.

        A tie is reported as no dominant reason on purpose. "Fix this file first" is
        advice, and advice that cannot pick between two files is not advice.
        """
        if not self.reasons:
            return None
        ranked = sorted(self.reasons.items(), key=lambda kv: (-kv[1], kv[0]))
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            return None
        return ranked[0][0]

    @property
    def remedy(self) -> str | None:
        """One line: the dominant failure, and the file that owns it."""
        reason = self.dominant_reason
        if reason is None:
            return None
        return REASON_REMEDY.get(reason, "no known remedy mapped for this reason")


def _parse(row: dict[str, object]) -> Correction | None:
    """One db row → one Correction, or None if the note is not a review verdict.

    Notes written by `resolve`/`drop` (free text the owner typed, or None) are not
    corrections and must not be counted as one; the SQL filters most of them out and this
    is the second gate, because a hand-typed note beginning "rejected:" is not impossible.
    """
    note = str(row["resolution_note"] or "")

    rejected = _REJECTED.match(note)
    if rejected is not None:
        # The category is taken as written rather than validated against REJECT_REASONS.
        # A reason this module has never heard of means the vocabulary grew and nobody
        # told the report; counting it under its own name (with no remedy mapped) keeps
        # the pile's size honest, where discarding it would quietly understate it.
        reason = rejected.group("reason")
        return Correction(
            commitment_id=int(row["commitment_id"]),  # type: ignore[call-overload]
            verdict="reject",
            reason=reason,
            # A reject leaves `confidence` untouched, so the column still holds what the
            # model said. Only the accept path overwrites it.
            confidence=float(row["confidence"]),  # type: ignore[arg-type]
            source=str(row["source"]),
            author=(str(row["author"]) if row["author"] else None),
            extraction_version=(
                str(row["extraction_version"]) if row["extraction_version"] else None
            ),
            corrected_at=str(row["corrected_at"]),
        )

    accepted = _ACCEPTED.match(note)
    if accepted is not None:
        try:
            confidence: float | None = float(accepted.group("confidence"))
        except ValueError:  # pragma: no cover — the pattern already constrains this
            confidence = None
        return Correction(
            commitment_id=int(row["commitment_id"]),  # type: ignore[call-overload]
            verdict="accept",
            reason=None,
            confidence=confidence,
            source=str(row["source"]),
            author=(str(row["author"]) if row["author"] else None),
            extraction_version=(
                str(row["extraction_version"]) if row["extraction_version"] else None
            ),
            corrected_at=str(row["corrected_at"]),
        )
    return None


def _since(days: int, now: datetime | None = None) -> str:
    """Start of the trailing window, ISO/UTC — the same clock the ledger stamps with."""
    return ((now or datetime.now(UTC)) - timedelta(days=days)).isoformat()


def _buckets(corrections: list[Correction]) -> list[Bucket]:
    """Accept/reject split per tenth of confidence, low to high.

    Bucketed by the floor of the tenth, so 0.70 lands in the 0.7 band and 0.79 with it —
    which matters because `confidence_threshold` defaults to exactly 0.7 and the band
    straddling the threshold is the one worth reading. Only bands with data are returned;
    printing empty rows implies a sample that was never taken.
    """
    counts: dict[float, list[int]] = {}
    for c in corrections:
        if c.confidence is None:
            continue
        low = min(0.9, max(0.0, int(c.confidence * 10) / 10))
        slot = counts.setdefault(low, [0, 0])
        slot[0 if c.verdict == "accept" else 1] += 1
    return [
        Bucket(low=low, accepted=slot[0], rejected=slot[1])
        for low, slot in sorted(counts.items())
    ]


def _rates(corrections: list[Correction], key: str, limit: int) -> list[SourceStat]:
    """Rejection rate per source/author, worst first, ties broken by volume."""
    tally: dict[str, list[int]] = {}
    for c in corrections:
        value = getattr(c, key) or "<unknown>"
        slot = tally.setdefault(str(value), [0, 0])
        slot[0] += 1
        if c.verdict == "reject":
            slot[1] += 1
    stats = [SourceStat(key=k, rejected=v[1], total=v[0]) for k, v in tally.items()]
    stats.sort(key=lambda s: (-s.reject_rate, -s.total, s.key))
    return stats[:limit]


def _cross(corrections: list[Correction], key: str) -> dict[str, dict[str, int]]:
    """Reject-reason counts split by `key` (source, or extraction_version).

    Both splits answer "is this one prompt's problem or everything's problem". The
    version split is only as good as the stamps in `source_item.extraction_version`: it
    is a real column the sync writes on every extracted item, so the join is honest, but
    a window that spans no prompt change produces one bucket and says nothing. The caller
    prints it only when there is more than one version in the window.
    """
    out: dict[str, dict[str, int]] = {}
    for c in corrections:
        if c.verdict != "reject" or c.reason is None:
            continue
        value = str(getattr(c, key) or "<unstamped>")
        out.setdefault(value, {})
        out[value][c.reason] = out[value].get(c.reason, 0) + 1
    return out


def report(
    conn: sqlite3.Connection, days: int = 30, now: datetime | None = None, limit: int = 8
) -> Report:
    """Everything the correction ledger can say about the last `days` days."""
    rows = conn.execute(query("corrections"), {"user_id": USER_ID, "since": _since(days, now)})
    corrections = [c for c in (_parse(dict(r)) for r in rows) if c is not None]

    reasons: dict[str, int] = {}
    for c in corrections:
        if c.reason is not None:
            reasons[c.reason] = reasons.get(c.reason, 0) + 1

    return Report(
        days=days,
        corrections=corrections,
        reasons=reasons,
        reasons_by_source=_cross(corrections, "source"),
        reasons_by_version=_cross(corrections, "extraction_version"),
        buckets=_buckets(corrections),
        worst_sources=_rates(corrections, "source", limit),
        worst_senders=_rates(corrections, "author", limit),
    )
