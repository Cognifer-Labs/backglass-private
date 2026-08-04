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

        # A move is aimed at where the plan WAS, not where it is going. Resolved against
        # the message like every other date here (rule 4); when the message never named
        # the old time this stays None and the move is matched like any other sighting,
        # which in practice means it becomes a new plan.
        moved_from = (
            dates.resolve_due(
                candidate.replaces_start_at, occurred_at=occurred_at, allow_past=True
            ).value
            if candidate.replaces_earlier
            else None
        )

        # ── step 5: dedup, against the ledger and within this one response.
        match = _match(
            candidate,
            entity_ids,
            starts.value,
            source_item_id,
            ledger,
            settings,
            accepted_here,
            moved_from=moved_from,
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
                aimed=moved_from is not None,
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
    aimed: bool,
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
        aimed=aimed,
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
    *,
    moved_from: str | None = None,
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
    # Everything below compares against `target`: the plan's OLD time for a move that
    # named one, and the candidate's own time otherwise. Ranking on the new time aimed a
    # reschedule at its destination and repainted whichever unrelated plan happened to sit
    # nearest to it, which is the opposite of what a move means.
    target = moved_from if moved_from is not None else starts_at
    best: tuple[tuple[int, int, int], int] | None = None
    for row in ledger.open_engagements():
        if int(row["id"]) in fresh:
            continue
        raw = row["entity_ids"]
        row_ids = {int(part) for part in str(raw).split(",")} if raw else set()
        # A message this row already cites is a message this row already accounts for —
        # re-extraction, not news. Its time must not be compared: after a reschedule the
        # row has moved on, and holding the original message to the new time would file
        # a duplicate of the plan it created. Wording and people still have to agree, so
        # a message that produced two plans still resolves to the right one (the nearest
        # start time breaks the tie below).
        cited = ledger.cites_engagement(int(row["id"]), source_item_id)
        if not _same_row(
            candidate, entity_ids, target, row, row_ids, settings, already_cited=cited
        ):
            continue
        # A cancelled plan stays visible so re-extraction cannot resurrect it, but it
        # must not become a sink that swallows real invitations for the rest of time.
        # The distinction is whether this exact message has been read against this row
        # before: if it has, this is re-extraction and there is nothing new to file; if
        # it has not, someone is proposing the thing again and that is a new plan.
        if str(row["status"]) == "declined" and not cited:
            continue
        stored = str(row["starts_at"]) if row["starts_at"] is not None else None
        # Ordered by how strongly this row is the one the message is about:
        #   0. a row this message is already cited on — it IS this message's plan, and a
        #      move that has already been applied has left the origin, so distance from
        #      the origin says nothing. Without this, re-extracting an applied move lost
        #      to whatever unrelated plan had since been booked into the vacated slot,
        #      and repainted that instead.
        #   1. distance from the origin, which is what a move is aimed at.
        #      In practice key 0 is the one that decides. Keys 1 and 2 rarely get to act,
        #      because `_same_row` has already gated on the target: an aimed move only
        #      agrees with rows on the origin's day. Mutating either of them survives the
        #      suite, and that is left recorded rather than papered over with a contrived
        #      test — they are a defensible ordering for the narrow case where several
        #      rows do agree, not load-bearing logic.
        #   2. distance from the destination, purely to break ties deterministically —
        #      with a day-precision origin two plans on that day tie at 0, and the one
        #      the new time points at is the better guess than the lower row id.
        rank = (
            0 if cited else 1,
            _minutes_apart(target, stored),
            _minutes_apart(starts_at, stored),
        )
        if best is None or rank < best[0]:
            best = (rank, int(row["id"]))
    return best[1] if best else None


def _minutes_apart(left: str | None, right: str | None) -> int:
    """How far apart two stored start times are, for choosing between agreeing rows.

    Zero whenever either side names no hour: the two are indistinguishable at day
    precision and the caller falls back to the first, which is all the information there
    is. Deliberately tolerant of anything unparseable — this only ranks candidates that
    have already agreed on wording, people and day, so the worst a bad parse can do is
    pick the older of two plans that were going to be confused anyway.
    """
    if left is None or right is None:
        return 0
    from datetime import date as _date
    from datetime import datetime

    try:
        if _has_clock(left) and _has_clock(right):
            first = datetime.fromisoformat(left)
            second = datetime.fromisoformat(right)
            # Only subtract times that are actually comparable. A stored `starts_at` may
            # or may not carry an offset — `2026-08-20T10:30:00-07:00` from Calendar.app
            # and `2026-08-20T17:30` from a message describing the same event — and
            # subtracting one from the other is a TypeError, which crashed a whole
            # backfill through the one code path in the pipeline that re-raises. There is
            # no honest way to compare them: assuming a zone for the naive side invents
            # the very offset the value is missing. So drop to the precision both sides
            # genuinely have, which is the day, exactly as the no-clock case does.
            if (first.tzinfo is None) == (second.tzinfo is None):
                delta = first - second
                return abs(int(delta.total_seconds() // 60))
        # At least one names only a day — or the two clocks are not comparable — so
        # compare days. Returning 0 here, which is what "no clock, no opinion" used to
        # do, made every same-wording plan equally near, so a re-read of a message that
        # produced a Friday plan and a Saturday plan resolved to whichever row came back
        # first and swapped the two.
        days = _date.fromisoformat(left[:10]) - _date.fromisoformat(right[:10])
        return abs(days.days) * 1440
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
        any_day=False,
    )


def _same_row(
    candidate: ExtractedEngagement,
    ids: set[int],
    starts_at: str | None,
    row: dict[str, object],
    row_ids: set[int],
    settings: Settings,
    *,
    already_cited: bool = False,
) -> bool:
    """A candidate against a row from an EARLIER message.

    The clock separates unless the message said it was moving something. Four rounds of
    verification established that the times alone cannot answer this: comparing them
    turned every reschedule into a second row that double-booked the day, and ignoring
    them let a 4pm plan repaint an unrelated 9am one out of existence. `replaces_earlier`
    is the sentence telling us which, and when it is absent the safe answer is "different
    plan" — a duplicate is visible on the board and can be dismissed, while a wrongly
    merged plan silently replaces one the owner had already agreed to.
    """
    return _agrees(
        candidate.what,
        ids,
        starts_at,
        str(row["what"]),
        row_ids,
        str(row["starts_at"]) if row["starts_at"] is not None else None,
        settings,
        # `already_cited` means this message has been read against this row before, so
        # the row may legitimately have moved since; nothing else may cross a day.
        any_day=already_cited,
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
    any_day: bool,
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
    # Note that `starts_at` here is the *target* the caller chose: a move's origin when
    # the message named one, and the candidate's own time otherwise. Comparing a move
    # against its destination is what let it repaint whichever plan sat near where it was
    # going rather than the plan it was leaving.
    if starts_at is not None and other_start is not None:
        # The one case where the stored time says nothing: this message has already been
        # read against this row, so the row may have moved on since.
        if any_day:
            return True
        if _has_clock(starts_at) and _has_clock(other_start):
            return starts_at[:16] == other_start[:16]
        return starts_at[:10] == other_start[:10]

    # At most one has a time. That is the case where a plan acquires its date — "we
    # should get dinner" then "how's Friday" — so the guest list is what carries the
    # match. Two plans that name nobody fall back to their wording alone, which is all
    # there is to go on.
    return bool(ids and other_ids) or not (ids or other_ids)
