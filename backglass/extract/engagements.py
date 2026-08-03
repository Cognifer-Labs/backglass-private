"""Turn the engagements half of a tier-2 response into ledger rows.

The commitment half of the same response is handled by `extract/commitments.py`, and this
module deliberately mirrors its shape: the same five post-processing steps, in code
rather than in the prompt, for the same reason — they must give the same answer twice for
the same input, which is what CLAUDE.md rule 3 requires of the whole sync.

Two things differ, and both follow from what a plan is rather than from taste:

  * A commitment has one counterparty; an engagement has a guest list. Resolution and
    dedup therefore work over sets of entities, not a single id.
  * A commitment's second sighting is noise to be recorded and dropped. A plan's second
    sighting is usually the answer — "Friday works" is what turns a proposal into an
    arrangement — so a dedup hit advances the row it matched instead of discarding the
    sentence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backglass.config import Settings
from backglass.extract import dates, entities
from backglass.extract.schemas import CommitmentExtraction, ExtractedEngagement
from backglass.ledger import Ledger

#: A status may only move forward. A message restating an agreed plan ("still on for
#: Friday?") reads as a proposal to the model, and letting that walk `confirmed` back to
#: `proposed` would drop the plan off the day's schedule on the strength of a pleasantry.
#: Declining is reachable from anywhere: a plan can be called off at any point.
ADVANCES_TO: dict[str, set[str]] = {
    "proposed": {"proposed", "confirmed", "declined"},
    "confirmed": {"confirmed", "declined"},
    "declined": {"declined"},
}


@dataclass
class EngagementReport:
    inserted: int = 0
    advanced: int = 0
    deduped: int = 0
    review_queue: int = 0
    date_notes: list[str] = field(default_factory=list)


def apply(
    extraction: CommitmentExtraction,
    *,
    source_item_id: int,
    occurred_at: str,
    ledger: Ledger,
    settings: Settings,
) -> EngagementReport:
    """Post-processing for the engagements in one tier-2 response."""
    report = EngagementReport()
    accepted_here: list[tuple[int, ExtractedEngagement, set[int], str | None]] = []

    for candidate in extraction.engagements:
        # ── step 1: every name becomes an entity. resolve_entity drops the owner, so a
        # plan the owner is obviously part of does not list them as their own guest.
        entity_ids = {
            resolved
            for raw in candidate.people
            if (resolved := ledger.resolve_entity(raw)) is not None
        }

        # ── date resolution (rule 4) before anything is stored: against the message's
        # own timestamp, never the run's.
        starts = dates.resolve_due(candidate.starts_at, occurred_at=occurred_at)
        ends = dates.resolve_due(candidate.ends_at, occurred_at=occurred_at)
        for resolution in (starts, ends):
            if resolution.note:
                report.date_notes.append(f"{candidate.what!r}: {resolution.note}")

        # ── step 5: dedup, against the ledger and within this one response.
        match = _match(
            candidate,
            entity_ids,
            starts.value,
            source_item_id,
            ledger,
            settings,
            accepted_here,
        )
        if match is not None:
            report.deduped += 1
            ledger.stats.engagements_deduped += 1
            # The sentence is kept whatever happens to the status: it is the evidence
            # for the state the row is now in.
            ledger.record_engagement_evidence(
                match, source_item_id, candidate.evidence, kind="restated"
            )
            # A restatement can also name someone the first sighting did not.
            for entity_id in entity_ids:
                ledger.link_engagement_person(match, entity_id)
            if _advance(
                candidate,
                match,
                starts.value,
                ends.value,
                ledger,
                settings,
                source_item_id=source_item_id,
                occurred_at=occurred_at,
            ):
                report.advanced += 1
            # Recorded exactly like an insert. A row this response has already touched is
            # spoken for, whether it was created here or matched here — and leaving the
            # matched ones out was the whole defect: "coffee Friday, 9am or 4pm" against
            # a ledger that already held the 9am one deduped BOTH candidates onto it, so
            # the 4pm plan was never written and the 9am one was repainted to 16:00.
            accepted_here.append((match, candidate, entity_ids, starts.value))
            continue

        engagement_id = ledger.insert_engagement(
            kind=candidate.kind,
            what=candidate.what,
            starts_at=starts.value,
            ends_at=ends.value,
            when_is_explicit=candidate.when_is_explicit,
            location=candidate.location,
            status=candidate.status,
            confidence=candidate.confidence,
            source_item_id=source_item_id,
            entity_ids=sorted(entity_ids),
            evidence=candidate.evidence,
        )
        report.inserted += 1
        accepted_here.append((engagement_id, candidate, entity_ids, starts.value))

        # ── step 3: below the threshold it is created but not believed. Rule 2: low
        # confidence goes to the review queue, never into the brief as fact.
        if candidate.confidence < settings.confidence_threshold:
            report.review_queue += 1

    return report


def _advance(
    candidate: ExtractedEngagement,
    engagement_id: int,
    starts_at: str | None,
    ends_at: str | None,
    ledger: Ledger,
    settings: Settings,
    *,
    source_item_id: int,
    occurred_at: str,
) -> bool:
    """Apply a later sighting to the plan it restates.

    Gated on confidence for the reason `commitments.apply` gates supersession: changing
    the state of an existing row is a heavier act than adding one. A wrong insert is
    visible on the board and can be dropped; a wrong `declined` makes a real plan vanish,
    and a wrong `confirmed` puts a block on the day for something nobody agreed to. Below
    the threshold the citation is still recorded, so the owner can see the sentence — the
    row just does not move on its say-so.
    """
    if candidate.confidence < settings.confidence_threshold:
        return False
    current = _status_of(engagement_id, ledger)
    if current is None:
        return False
    status = candidate.status if candidate.status in ADVANCES_TO[current] else current
    newest = ledger.newest_citation_before(engagement_id, source_item_id)
    return ledger.advance_engagement(
        engagement_id,
        status=status,
        starts_at=starts_at,
        ends_at=ends_at,
        location=candidate.location,
        may_repaint=_at_least_as_recent(occurred_at, newest),
    )


def _at_least_as_recent(sighting: str, newest: str | None) -> bool:
    """Is this message no older than the newest one already cited on the plan?

    Compared as instants, not as strings: these timestamps carry the sender's own offset,
    so "2026-07-15T09:00:00-07:00" and "2026-07-15T20:00:00+05:30" are the same moment and
    string order would disagree. An unparseable pair defaults to True — the pre-existing
    behaviour, and a repaint is recoverable where refusing one loses a correction.
    """
    if newest is None:
        return True
    from datetime import datetime

    try:
        left, right = datetime.fromisoformat(sighting), datetime.fromisoformat(newest)
    except ValueError:
        return True
    if (left.tzinfo is None) != (right.tzinfo is None):
        return True  # cannot compare a wall clock with an instant; do not guess
    return left >= right


def _status_of(engagement_id: int, ledger: Ledger) -> str | None:
    for row in ledger.open_engagements():
        if int(row["id"]) == engagement_id:
            return str(row["status"])
    return None


def _match(
    candidate: ExtractedEngagement,
    entity_ids: set[int],
    starts_at: str | None,
    source_item_id: int,
    ledger: Ledger,
    settings: Settings,
    accepted: list[tuple[int, ExtractedEngagement, set[int], str | None]],
) -> int | None:
    """The id of the live plan this candidate is another sighting of, or None.

    Three signals, because no one of them is enough on its own. Text alone would fuse
    every "coffee" the owner ever arranges; people alone would fuse two unrelated plans
    with the same friend; a day alone would fuse a lunch and an evening lecture. The rule
    is: the wording has to match, and the people or the day has to agree.

    An undated plan matches a dated one on purpose — "we should get dinner" followed by
    "how's Friday" is the same dinner, and the second sighting is precisely how it
    acquires its date.

    The clock matters differently depending on where the other sighting came from, and
    getting that backwards produced a duplicate that double-booked the day:

      * **Inside one response** the hour separates. A message that says "coffee at 9 or
        4" describes two possible plans, and the model returns two engagements; fusing
        them on the shared day would drop one.
      * **Across messages** the hour is the thing most likely to have been corrected.
        "Dinner Friday at 7" then "can we push to 7:30" is one dinner, and treating the
        new time as a new plan leaves two overlapping blocks on the day with no way for
        the owner to tell which is stale — the brief does not even print the hour. So a
        later sighting matches on the day and *repaints* the time.
    """
    for engagement_id, other, other_ids, other_start in accepted:
        if _same(candidate, entity_ids, starts_at, other, other_ids, other_start, settings):
            return engagement_id

    # Rows this same response already created are settled above, at clock precision.
    # They are also in the ledger by now — insert_engagement writes immediately — and the
    # loop below compares on the day, so without this the 4pm coffee would match the 9am
    # one it was just distinguished from and repaint it.
    fresh = {engagement_id for engagement_id, _, _, _ in accepted}
    # More than one stored plan can agree on wording, people and day — that is exactly
    # what "coffee Friday, 9am or 4pm" leaves behind — so agreeing is not enough to pick
    # one. Taking the first row in id order repainted the confirmed 9am coffee when a
    # later message settled the 4pm one. Collect every candidate row and take the nearest
    # start time; an exact time match therefore always wins, and a reschedule with no
    # exact match still lands on the plan it is closest to rather than the oldest.
    best: tuple[int, int] | None = None
    for row in ledger.open_engagements():
        if int(row["id"]) in fresh:
            continue
        raw = row["entity_ids"]
        row_ids = {int(part) for part in str(raw).split(",")} if raw else set()
        if not _same_row(candidate, entity_ids, starts_at, row, row_ids, settings):
            continue
        # A cancelled plan stays visible so re-extraction cannot resurrect it, but it
        # must not become a sink that swallows real invitations for the rest of time.
        # The distinction is whether this exact message has been read against this row
        # before: if it has, this is re-extraction and there is nothing new to file; if
        # it has not, someone is proposing the thing again and that is a new plan.
        if str(row["status"]) == "declined" and not ledger.cites_engagement(
            int(row["id"]), source_item_id
        ):
            continue
        stored = str(row["starts_at"]) if row["starts_at"] is not None else None
        distance = _minutes_apart(starts_at, stored)
        if best is None or distance < best[0]:
            best = (distance, int(row["id"]))
    return best[1] if best else None


def _minutes_apart(left: str | None, right: str | None) -> int:
    """How far apart two stored start times are, for choosing between agreeing rows.

    Zero whenever either side names no hour: the two are indistinguishable at day
    precision and the caller falls back to the first, which is all the information there
    is. Deliberately tolerant of anything unparseable — this only ranks candidates that
    have already agreed on wording, people and day, so the worst a bad parse can do is
    pick the older of two plans that were going to be confused anyway.
    """
    if left is None or right is None or not _has_clock(left) or not _has_clock(right):
        return 0
    try:
        from datetime import datetime

        return abs(
            int(
                (
                    datetime.fromisoformat(left) - datetime.fromisoformat(right)
                ).total_seconds()
                // 60
            )
        )
    except ValueError:
        return 0


def _same(
    candidate: ExtractedEngagement,
    ids: set[int],
    starts_at: str | None,
    other: ExtractedEngagement,
    other_ids: set[int],
    other_start: str | None,
    settings: Settings,
) -> bool:
    """Two engagements in the SAME response — the hour separates them."""
    return _agrees(
        candidate.what,
        ids,
        starts_at,
        other.what,
        other_ids,
        other_start,
        settings,
        to_the_hour=True,
    )


def _same_row(
    candidate: ExtractedEngagement,
    ids: set[int],
    starts_at: str | None,
    row: dict[str, object],
    row_ids: set[int],
    settings: Settings,
) -> bool:
    """A candidate against a row from an EARLIER message — the day decides, because the
    hour is the part a later message most often corrects."""
    return _agrees(
        candidate.what,
        ids,
        starts_at,
        str(row["what"]),
        row_ids,
        str(row["starts_at"]) if row["starts_at"] is not None else None,
        settings,
        to_the_hour=False,
    )


def _has_clock(value: str) -> bool:
    """Does this stored time name an hour, or only a day?

    `dates.resolve_due` returns a bare `YYYY-MM-DD` when the message stated no time and
    an ISO datetime when it did, so the separator is the whole test.
    """
    return "T" in value or " " in value


def _agrees(
    what: str,
    ids: set[int],
    starts_at: str | None,
    other_what: str,
    other_ids: set[int],
    other_start: str | None,
    settings: Settings,
    *,
    to_the_hour: bool,
) -> bool:
    if entities.similar(what, other_what) < settings.dedup_threshold:
        return False

    # Named on both sides with nobody in common: dinner with Priya and dinner with Sam
    # are two dinners however alike the wording is.
    if ids and other_ids and not (ids & other_ids):
        return False

    # Both pinned to a time, so the time decides — and it has to, because the people can
    # never separate a standing arrangement. Weekly coffee with the same friend is a new
    # plan each week; letting the shared guest fuse them would keep one row and silently
    # swallow every later occurrence.
    #
    # `to_the_hour` is the caller saying which question this is: two candidates in one
    # response are separated by their clock times, while a candidate against a stored row
    # is compared on the day so that a corrected time repaints rather than duplicating.
    if starts_at is not None and other_start is not None:
        if to_the_hour and _has_clock(starts_at) and _has_clock(other_start):
            return starts_at[:16] == other_start[:16]
        return starts_at[:10] == other_start[:10]

    # At most one has a time. That is the case where a plan acquires its date — "we
    # should get dinner" then "how's Friday" — so the guest list is what carries the
    # match. Two plans that name nobody fall back to their wording alone, which is all
    # there is to go on.
    return bool(ids and other_ids) or not (ids or other_ids)
