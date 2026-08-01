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
from typing import Any

from backglass.config import Settings
from backglass.connectors.base import SourceItem
from backglass.db import now_iso, query

USER_ID = 1  # docs/03: "user_id on every table, always 1."


@dataclass
class LedgerStats:
    source_items_inserted: int = 0
    source_items_unchanged: int = 0
    source_item_conflicts: list[str] = field(default_factory=list)
    entities_created: int = 0
    commitments_inserted: int = 0
    commitments_deduped: int = 0
    commitments_superseded: int = 0
    triage_recorded: int = 0


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

        if email:
            hit = self.conn.execute(
                "SELECT id FROM entity WHERE user_id = ? AND kind = 'person' "
                "AND EXISTS (SELECT 1 FROM json_each(entity.aliases_json) "
                "            WHERE lower(json_each.value) = ?)",
                (USER_ID, email),
            ).fetchone()
            if hit:
                return int(hit["id"])
        hit = self.conn.execute(
            "SELECT id FROM entity WHERE user_id = ? AND kind = 'person' "
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
    ) -> int:
        self.stats.commitments_inserted += 1
        self.writes += 1
        if self.dry_run:
            return self._pseudo_id()
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
        return int(cursor.lastrowid or 0)

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
