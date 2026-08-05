"""The write layer. Every mutation the pipeline makes goes through here.

Two reasons this is one class rather than scattered SQL:

  - `--dry-run` is a hard requirement (docs/10 §CLI), not a convenience. One place that
    knows whether to actually write is far easier to trust than a flag threaded through
    eight modules.
  - CLAUDE.md rule 3: "Two consecutive runs with no upstream changes produce zero writes.
    If you cannot assert this in a test, the sync is wrong." `writes` is that assertion's
    subject, and it only counts if every write increments it.

`writes` counts domain writes — source_item, entity, commitment. The `run` row itself is
telemetry and is excluded, otherwise the idempotency assertion could never be zero.
See tasks/todo.md §Deviations #7.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from backglass.config import Settings
from backglass.connectors.base import SourceItem
from backglass.db import now_iso, query

USER_ID = 1  # docs/03: "user_id on every table, always 1."


class LedgerError(ValueError):
    """A write the ledger refuses. One type, so a caller catching it catches all of it."""


def _check_due_at(due_at: str | None) -> None:
    """A due date is a date, in the one format the rest of the system reads.

    `YYYY-MM-DD` or a full ISO timestamp — the two shapes `extract/dates.resolve_due`
    produces, and the two the board's queries can order by. NULL stays legal: a
    commitment with no deadline is an ordinary thing.
    """
    if due_at is None:
        return
    text = str(due_at)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise LedgerError(
            f"{text[:60]!r} is not a due date; use YYYY-MM-DD or a full timestamp"
        ) from None


def _sharpens(new: str | None, current: str | None, *, aimed: bool = False) -> bool:
    """Is `new` worth writing over `current` for an engagement's time?

    Yes when there was nothing there, and yes when a later message moves a time that was
    already known — a reschedule is the single most common thing a follow-up message
    does. No when `new` is absent, unchanged, or *less* precise than what is stored: a
    message that mentions only the day must not blank an hour an earlier one established,
    or every passing reference to a plan would erode it.
    """
    if new is None or new == current:
        return False
    if current is None:
        return True
    # `aimed` means the message named the plan it was moving, so its new time is a
    # statement about that plan rather than a passing reference to it. "Push Friday's
    # dinner to Saturday, I'll pin a time later" is day-precision on purpose, and the
    # precision rule below would silently refuse it — leaving the plan on Friday with the
    # match already consumed, so not even a duplicate row appears to show the loss.
    if aimed:
        return True
    has_clock = "T" in new or " " in new
    had_clock = "T" in current or " " in current
    return has_clock or not had_clock


@dataclass
class LedgerStats:
    source_items_inserted: int = 0
    source_items_unchanged: int = 0
    source_item_conflicts: list[str] = field(default_factory=list)
    entities_created: int = 0
    commitments_inserted: int = 0
    commitments_deduped: int = 0
    commitments_superseded: int = 0
    evidence_recorded: int = 0
    triage_recorded: int = 0
    engagements_inserted: int = 0
    engagements_deduped: int = 0
    engagements_advanced: int = 0


class Ledger:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, *, dry_run: bool = False):
        self.conn = conn
        self.settings = settings
        self.dry_run = dry_run
        self.writes = 0
        self.stats = LedgerStats()
        self._next_pseudo_id = -1

    def _pseudo_id(self) -> int:
        """Dry-run stand-in for a rowid, so downstream code can still be exercised."""
        value = self._next_pseudo_id
        self._next_pseudo_id -= 1
        return value

    # ────────────────────────────────────────────────────────── source items

    def upsert_source_item(self, item: SourceItem) -> tuple[int, bool]:
        """Insert if new. Returns (id, written).

        A matching content_hash short-circuits before any model is invoked, which is the
        whole cost model. A *differing* hash for an already-stored external_id is not an
        update: source_item is immutable (docs/03, enforced by a trigger in migration
        0002). Gmail message bodies do not change, so this means either the quote-stripper
        changed or something upstream is lying. Either way it is recorded and skipped,
        not silently applied.
        """
        existing = self.conn.execute(
            "SELECT id, content_hash FROM source_item "
            "WHERE user_id = ? AND source = ? AND external_id = ?",
            (USER_ID, item.source, item.external_id),
        ).fetchone()

        if existing is not None:
            if existing["content_hash"] != item.content_hash:
                self.stats.source_item_conflicts.append(f"{item.source}:{item.external_id}")
            self.stats.source_items_unchanged += 1
            return int(existing["id"]), False

        self.stats.source_items_inserted += 1
        self.writes += 1
        if self.dry_run:
            return self._pseudo_id(), True

        from backglass.extract import templates

        cursor = self.conn.execute(
            "INSERT INTO source_item "
            "(user_id, source, external_id, fetched_at, occurred_at, author, title, "
            " body_text, raw_json, content_hash, template_hash, triage_verdict) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                USER_ID,
                item.source,
                item.external_id,
                now_iso(),
                item.occurred_at,
                item.author,
                item.title,
                item.body_text,
                item.raw_json,
                item.content_hash,
                # Derived at ingest, like content_hash but for the item's *shape*.
                # Not part of the conflict check — recomputable, never authored.
                templates.template_hash(
                    author=item.author, title=item.title, body_text=item.body_text
                ),
            ),
        )
        return int(cursor.lastrowid or 0), True

    def record_triage(self, source_item_id: int, verdict: str, reason: str | None) -> bool:
        """Write the triage verdict. No-op if it is already what we would write."""
        current = self.conn.execute(
            "SELECT triage_verdict, triage_reason FROM source_item WHERE id = ?",
            (source_item_id,),
        ).fetchone()
        if current is not None and current["triage_verdict"] == verdict:
            return False
        self.stats.triage_recorded += 1
        self.writes += 1
        if self.dry_run:
            return True
        self.conn.execute(
            "UPDATE source_item SET triage_verdict = ?, triage_reason = ? WHERE id = ?",
            (verdict, reason, source_item_id),
        )
        return True

    def record_extraction_version(self, source_item_id: int, version: str) -> bool:
        current = self.conn.execute(
            "SELECT extraction_version FROM source_item WHERE id = ?", (source_item_id,)
        ).fetchone()
        if current is not None and current["extraction_version"] == version:
            return False
        self.writes += 1
        if self.dry_run:
            return True
        self.conn.execute(
            "UPDATE source_item SET extraction_version = ? WHERE id = ?",
            (version, source_item_id),
        )
        return True

    # ────────────────────────────────────────────────────────────── entities

    def resolve_entity(self, raw: str | None) -> int | None:
        """Post-processing step 1: resolve a counterparty to an entity, create on miss.

        docs/03: "Resolution merges 'Dave', 'David R.', and drodriguez@… into one entity.
        Get this wrong and the 'awaiting others' view fragments into duplicates and stops
        being useful." The email address is the reliable key, so it becomes an alias and
        is matched first; the display name is only a fallback.
        """
        if not raw or not raw.strip():
            return None
        from backglass.extract.entities import parse_counterparty

        name, email = parse_counterparty(raw)

        # The owner is never their own counterparty. People mail themselves reminders
        # constantly, and without this the ledger grows an `entity` row for the owner and
        # commitments they owe to themselves — which then read as "awaiting others" in the
        # one view docs/03 says justifies the whole build. A self-addressed task is still a
        # real commitment; it just has no counterparty, and the column is nullable.
        if self.settings.owns(email or raw):
            return None

        # Both kinds: a counterparty reclassified to 'org' (Sallie Mae, a housing
        # portal) must keep resolving to its row, or every later extraction creates
        # a duplicate 'person' and the awaiting view fragments.
        if email:
            hit = self.conn.execute(
                "SELECT id FROM entity WHERE user_id = ? AND kind IN ('person', 'org') "
                "AND EXISTS (SELECT 1 FROM json_each(entity.aliases_json) "
                "            WHERE lower(json_each.value) = ?)",
                (USER_ID, email),
            ).fetchone()
            if hit:
                return int(hit["id"])
        hit = self.conn.execute(
            "SELECT id FROM entity WHERE user_id = ? AND kind IN ('person', 'org') "
            "AND lower(canonical_name) = ?",
            (USER_ID, name.lower()),
        ).fetchone()
        if hit:
            return int(hit["id"])

        aliases = [a for a in (email,) if a]
        self.stats.entities_created += 1
        self.writes += 1
        if self.dry_run:
            return self._pseudo_id()
        cursor = self.conn.execute(
            "INSERT INTO entity (user_id, kind, canonical_name, aliases_json) "
            "VALUES (?, 'person', ?, ?)",
            (USER_ID, name, json.dumps(aliases)),
        )
        return int(cursor.lastrowid or 0)

    # ─────────────────────────────────────────────────────────── commitments

    def open_commitments_for(
        self, direction: str, entity_id: int | None
    ) -> list[dict[str, Any]]:
        return list(
            self.conn.execute(
                query("open_commitments_for_dedup"),
                {
                    "user_id": USER_ID,
                    "direction": direction,
                    "counterparty_entity_id": entity_id,
                },
            )
        )

    def insert_commitment(
        self,
        *,
        direction: str,
        entity_id: int | None,
        what: str,
        due_at: str | None,
        estimated_minutes: int | None,
        estimate_source: str | None,
        confidence: float,
        source_item_id: int,
        evidence: str | None = None,
        evidence_kind: str = "original",
    ) -> int:
        """`evidence` is the verbatim sentence the claim rests on (schemas.py).

        It is written here, next to the row it justifies, rather than by the caller:
        there are two doors into this table — extraction and the dashboard's quick-add —
        and a citation written at one of them is not a citation rule, it is a coincidence.

        `due_at` is checked here for exactly that reason. Extraction resolves its dates
        through `extract/dates.resolve_due`, which cannot return a string this rejects;
        quick-add put the form field straight into the column, so `tomorrow`,
        `2026-02-30`, `9999-99-99` and three hundred characters of `x` all became due
        dates in a column that every board query sorts and compares by. The door that
        was wrong is fixed too, but the guard belongs at the INSERT — the next door has
        not been written yet.
        """
        _check_due_at(due_at)
        self.stats.commitments_inserted += 1
        self.writes += 1
        if self.dry_run:
            commitment_id = self._pseudo_id()
            self.record_evidence(
                commitment_id, source_item_id, evidence, kind=evidence_kind
            )
            return commitment_id
        cursor = self.conn.execute(
            "INSERT INTO commitment "
            "(user_id, direction, counterparty_entity_id, what, due_at, estimated_minutes, "
            " estimate_source, confidence, status, source_item_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (
                USER_ID,
                direction,
                entity_id,
                what,
                due_at,
                estimated_minutes,
                estimate_source,
                confidence,
                source_item_id,
                now_iso(),
            ),
        )
        commitment_id = int(cursor.lastrowid or 0)
        self.record_evidence(commitment_id, source_item_id, evidence, kind=evidence_kind)
        return commitment_id

    def record_evidence(
        self,
        commitment_id: int,
        source_item_id: int,
        quote: str | None,
        *,
        kind: str = "restated",
    ) -> bool:
        """Cite a source item for a commitment. Returns True when a row was written.

        Idempotent on (commitment, source_item), which is what keeps rule 3 true: the
        second sync over an unchanged item conflicts and writes nothing, so `writes`
        still lands on zero. The write is only counted when a row actually appears —
        counting the attempt would make an idempotent re-read look like work.

        A citation is never updated in place. If the same document is read again and the
        model quotes a different sentence from it, the first quote is what the owner
        already saw on the board, and rewriting it under them is the kind of silent
        change docs/03's immutability rule exists to prevent.
        """
        if self.dry_run:
            self.stats.evidence_recorded += 1
            self.writes += 1
            return True
        cursor = self.conn.execute(
            "INSERT INTO commitment_evidence "
            "(user_id, commitment_id, source_item_id, quote, kind, seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, commitment_id, source_item_id) DO NOTHING",
            (USER_ID, commitment_id, source_item_id, quote, kind, now_iso()),
        )
        if cursor.rowcount != 1:
            return False
        self.stats.evidence_recorded += 1
        self.writes += 1
        return True

    # ───────────────────────────────────────────────────────────── engagements

    def open_engagements(self) -> list[dict[str, Any]]:
        """Plans the dedup pass in extract/engagements.py must be able to see.

        Includes `declined`, which is not "open" in any other sense — the name is kept
        for its callers. A cancelled plan has to stay visible to dedup or re-extraction
        files the invitation that arranged it as a fresh proposal; see the query.

        Unscoped where the commitment equivalent is scoped to (direction, counterparty),
        because an engagement's participants live in a child table and the message being
        read may name a different subset of them than the message that created the row —
        "dinner with Priya and Sam" then "see you Friday, Priya" is one plan mentioned
        twice. Matching happens in Python over the returned rows; the population is
        small by construction, since a plan stops being live once it happens.
        """
        return list(self.conn.execute(query("open_engagements"), {"user_id": USER_ID}))

    def insert_engagement(
        self,
        *,
        kind: str,
        what: str,
        starts_at: str | None,
        ends_at: str | None,
        when_is_explicit: bool,
        location: str | None,
        status: str,
        confidence: float,
        source_item_id: int,
        entity_ids: list[int],
        evidence: str | None = None,
        evidence_kind: str = "original",
    ) -> int:
        """Write a plan and everyone in it, citing the sentence it rests on.

        The people links and the citation are written here rather than by the caller for
        the reason `insert_commitment` gives: a rule applied at one of several call sites
        is not a rule. Every door into this table leaves a row that knows who is going
        and which sentence said so.
        """
        self.stats.engagements_inserted += 1
        self.writes += 1
        if self.dry_run:
            engagement_id = self._pseudo_id()
            self.record_engagement_evidence(
                engagement_id, source_item_id, evidence, kind=evidence_kind
            )
            return engagement_id
        cursor = self.conn.execute(
            "INSERT INTO engagement "
            "(user_id, kind, what, starts_at, ends_at, when_is_explicit, location, "
            " status, confidence, source_item_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                USER_ID,
                kind,
                what,
                starts_at,
                ends_at,
                int(when_is_explicit),
                location,
                status,
                confidence,
                source_item_id,
                now_iso(),
            ),
        )
        engagement_id = int(cursor.lastrowid or 0)
        for entity_id in entity_ids:
            self.link_engagement_person(engagement_id, entity_id)
        self.record_engagement_evidence(
            engagement_id, source_item_id, evidence, kind=evidence_kind
        )
        return engagement_id

    def link_engagement_person(self, engagement_id: int, entity_id: int) -> bool:
        """Idempotent on (engagement, entity). Returns True when a row was written.

        A second sync over the same message re-resolves the same people and must not
        grow the guest list, so the conflict is the no-op rather than an error.
        """
        if self.dry_run:
            return True
        cursor = self.conn.execute(
            "INSERT INTO engagement_person (user_id, engagement_id, entity_id) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT (user_id, engagement_id, entity_id) DO NOTHING",
            (USER_ID, engagement_id, entity_id),
        )
        if cursor.rowcount != 1:
            return False
        self.writes += 1
        return True

    def record_engagement_evidence(
        self,
        engagement_id: int,
        source_item_id: int,
        quote: str | None,
        *,
        kind: str = "restated",
    ) -> bool:
        """Cite a source item for a plan. Idempotent on (engagement, source_item).

        The counting rule is `record_evidence`'s, for the same reason: a write is
        counted only when a row actually lands, so an unchanged second sync still
        totals zero and rule 3 holds.
        """
        if self.dry_run:
            self.stats.evidence_recorded += 1
            self.writes += 1
            return True
        cursor = self.conn.execute(
            "INSERT INTO engagement_evidence "
            "(user_id, engagement_id, source_item_id, quote, kind, seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, engagement_id, source_item_id) DO NOTHING",
            (USER_ID, engagement_id, source_item_id, quote, kind, now_iso()),
        )
        if cursor.rowcount != 1:
            return False
        self.stats.evidence_recorded += 1
        self.writes += 1
        return True

    def cites_engagement(self, engagement_id: int, source_item_id: int) -> bool:
        """Has this message already been read against this plan?

        The one question that separates re-extraction from a fresh invitation. A
        cancelled plan has to stay visible to dedup so a prompt-version bump cannot
        resurrect it, but a `declined` row that matches everything forever becomes a sink:
        someone proposing the same thing again months later would be filed onto the dead
        row and reach no surface at all. A citation for this exact source item means the
        ledger has seen this sentence before.
        """
        row = self.conn.execute(
            "SELECT 1 FROM engagement_evidence "
            "WHERE user_id = ? AND engagement_id = ? AND source_item_id = ?",
            (USER_ID, engagement_id, source_item_id),
        ).fetchone()
        return row is not None

    def newest_citation_before(self, engagement_id: int, source_item_id: int) -> str | None:
        """When the most recent OTHER message about this plan was sent.

        Used to keep an older sighting from repainting what a newer one established. The
        current message is excluded because its citation is written before the advance is
        attempted, so including it would compare the message against itself and always
        win.
        """
        rows = self.conn.execute(
            "SELECT s.occurred_at AS occurred_at FROM engagement_evidence e "
            "JOIN source_item s ON s.id = e.source_item_id "
            "WHERE e.user_id = ? AND e.engagement_id = ? AND e.source_item_id != ?",
            (USER_ID, engagement_id, source_item_id),
        ).fetchall()
        # Maximised in Python, over parsed instants. SQL's MAX() on this column is a
        # STRING comparison, and these timestamps carry each sender's own offset — so
        # across the owner's UTC-7 / UTC+5:30 split "2026-07-16T01:00+05:30" sorts above
        # "2026-07-15T20:00-07:00" while being half a day earlier. Picking the wrong
        # newest citation lets a stale message repaint a time a later one corrected.
        newest: datetime | None = None
        newest_raw: str | None = None
        for row in rows:
            raw = str(row["occurred_at"] or "")
            try:
                moment = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if moment.tzinfo is None:
                continue  # not comparable with an instant; ignore rather than guess
            if newest is None or moment > newest:
                newest, newest_raw = moment, raw
        return newest_raw

    def advance_engagement(
        self,
        engagement_id: int,
        *,
        status: str,
        starts_at: str | None = None,
        ends_at: str | None = None,
        location: str | None = None,
        may_repaint: bool = True,
        aimed: bool = False,
    ) -> bool:
        """Move a plan forward as later messages settle it. Returns True on a change.

        A NULL time learns a time, a `proposed` plan becomes `confirmed`, and a stated
        time that has *changed* is repainted — "can we push dinner to 7:30" is the same
        dinner, and refusing to move it meant either keeping the wrong hour or (worse,
        once dedup started separating on the clock) growing a second row that
        double-booked the day.

        What it will not do is let a vaguer sighting erase a sharper one. A later message
        that names only the day cannot blank an hour an earlier message established, and
        a NULL never overwrites a value — otherwise every passing mention of a plan would
        degrade what is known about it. The caller decides whether the status move is
        legal (see engagements.ADVANCES_TO) and gates all of this on confidence.

        Returns False when nothing would change, which is what keeps an unchanged re-read
        at zero writes.
        """
        row = self.conn.execute(
            "SELECT status, starts_at, ends_at, location FROM engagement "
            "WHERE user_id = ? AND id = ?",
            (USER_ID, engagement_id),
        ).fetchone()
        if row is None:
            return False

        updates: dict[str, Any] = {}
        if status != row["status"]:
            updates["status"] = status
        for column, value in (("starts_at", starts_at), ("ends_at", ends_at)):
            if not _sharpens(value, row[column], aimed=aimed):
                continue
            # Filling a hole is always safe; moving a time that is already known is only
            # safe from the newest message about the plan. Without that, re-extraction
            # replayed an old "Friday at 7" over the "push it to 7:30" that superseded it
            # and the row ping-ponged on every version bump.
            if row[column] is not None and not may_repaint:
                continue
            updates[column] = value
        # Location has no precision to compare; fill a hole, never repaint a wall.
        if location is not None and row["location"] is None:
            updates["location"] = location
        if not updates:
            return False

        self.stats.engagements_advanced += 1
        self.writes += 1
        if self.dry_run:
            return True
        assignments = ", ".join(f"{column} = ?" for column in updates)
        self.conn.execute(
            f"UPDATE engagement SET {assignments} WHERE user_id = ? AND id = ?",
            (*updates.values(), USER_ID, engagement_id),
        )
        return True

    def supersede(self, commitment_id: int, superseded_by: int) -> None:
        """Post-processing step 4. Never delete — docs/03 §Retention is explicit."""
        self.stats.commitments_superseded += 1
        self.writes += 1
        if self.dry_run:
            return
        self.conn.execute(
            "UPDATE commitment SET status = 'superseded', superseded_by = ?, resolved_at = ? "
            "WHERE id = ? AND status = 'open'",
            (superseded_by, now_iso(), commitment_id),
        )
