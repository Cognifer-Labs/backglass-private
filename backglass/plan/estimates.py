"""Effort estimates. docs/04 §1.3.

    "Every open commitment carries `estimated_minutes`. Without it the planner cannot pack
    a day."

Three sources, recorded in `commitment.estimate_source` so they can be told apart:

    extracted     the source text supported a number ("this'll take an hour")
    manual        the owner set it; sticky, and never overwritten by a default
    type_default  inferred from the commitment type

This closes the Phase 1 deviation that left `estimated_minutes` NULL: docs/04 is the
document that specifies the defaults, and it is this phase.

docs/04 §1.3 also says: "Track actuals ... and let the owner adjust the type defaults. Do
not auto-adjust silently." `ratio_report` computes the number; nothing here writes it
back. A planner that silently retunes its own estimates cannot be reasoned about when it
starts getting days wrong.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from backglass.config import Settings
from backglass.ledger import USER_ID

#: docs/04 §1.3 lists the types; the keyword sets are the mapping from a commitment's
#: text to one of them. Ordered — the first match wins, so the more specific patterns
#: come first.
TYPE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("meeting_prep", re.compile(r"\b(prep|agenda|brief(ing)?|pre-?read|deck for)\b", re.I)),
    (
        "decision",
        re.compile(r"\b(decide|decision|approve|approval|sign off|sign-off|choose)\b", re.I),
    ),
    ("review", re.compile(r"\b(review|read|look over|feedback on|comment on|check)\b", re.I)),
    (
        "draft",
        re.compile(r"\b(draft|write|prepare|put together|scope|plan|proposal|spec)\b", re.I),
    ),
]

#: docs/04 §1.3: "After 30 completed items, report the ratio of estimated to actual."
RATIO_MIN_SAMPLE = 30


@dataclass(frozen=True)
class Estimate:
    minutes: int
    source: str  # extracted | manual | type_default
    kind: str


def defaults(settings: Settings) -> dict[str, int]:
    table: dict[str, int] = {}
    for entry in settings.estimate_defaults:
        name, _, value = entry.partition(":")
        try:
            table[name.strip()] = int(value)
        except ValueError:
            continue
    table.setdefault("unknown", 45)
    return table


def classify(what: str) -> str:
    """Which type default applies. `unknown` when nothing matches, per docs/04 §1.3."""
    for kind, pattern in TYPE_PATTERNS:
        if pattern.search(what or ""):
            return kind
    return "unknown"


def estimate_for(
    what: str,
    settings: Settings,
    *,
    extracted_minutes: int | None = None,
    existing_minutes: int | None = None,
    existing_source: str | None = None,
) -> Estimate:
    """Resolve an estimate, respecting stickiness.

    "The owner can override on any item, and an override is sticky for that item." A
    manual estimate is therefore returned untouched — a later change to the type-default
    table must not silently overwrite a number a person chose.
    """
    kind = classify(what)
    if existing_source == "manual" and existing_minutes is not None:
        return Estimate(minutes=existing_minutes, source="manual", kind=kind)
    if extracted_minutes is not None:
        return Estimate(minutes=extracted_minutes, source="extracted", kind=kind)
    if existing_source == "extracted" and existing_minutes is not None:
        return Estimate(minutes=existing_minutes, source="extracted", kind=kind)
    return Estimate(minutes=defaults(settings)[kind], source="type_default", kind=kind)


def backfill(conn: sqlite3.Connection, settings: Settings) -> int:
    """Give every open commitment an estimate. Returns how many were filled.

    Only touches rows where `estimated_minutes` is NULL, so manual and extracted values
    survive. Run by the planner before it packs a day, because P1 says capacity is a hard
    constraint and an unestimated item cannot be measured against it.
    """
    rows = conn.execute(
        "SELECT id, what, estimated_minutes, estimate_source FROM commitment "
        "WHERE user_id = ? AND status = 'open' AND estimated_minutes IS NULL",
        (USER_ID,),
    ).fetchall()
    for row in rows:
        estimate = estimate_for(str(row["what"]), settings)
        conn.execute(
            "UPDATE commitment SET estimated_minutes = ?, estimate_source = ? WHERE id = ?",
            (estimate.minutes, estimate.source, row["id"]),
        )
    return len(rows)


@dataclass(frozen=True)
class RatioReport:
    sample: int
    estimated_minutes: int
    actual_minutes: int

    @property
    def ready(self) -> bool:
        return self.sample >= RATIO_MIN_SAMPLE

    @property
    def ratio(self) -> float:
        return (self.actual_minutes / self.estimated_minutes) if self.estimated_minutes else 0.0

    def sentence(self) -> str | None:
        """One line for the Friday retro. None until there is enough sample to mean anything."""
        if not self.ready or not self.estimated_minutes:
            return None
        if self.ratio > 1.05:
            return (
                f"Estimates run {self.ratio:.1f}x short over {self.sample} items. "
                "Raise the type defaults if that holds."
            )
        if self.ratio < 0.95:
            return (
                f"Estimates run {1 / self.ratio:.1f}x long over {self.sample} items. "
                "Lower the type defaults if that holds."
            )
        return f"Estimates are within 5% of actual over {self.sample} items."


def ratio_report(conn: sqlite3.Connection) -> RatioReport:
    """Estimated versus actual over completed plan blocks.

    Actual is the block's own duration, which is what the owner scheduled and then marked
    done. It is a proxy — the owner may have finished early and not said — but it is the
    only signal available without a timer, and docs/04 §6 rules timers out as a different
    product.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n, "
        "  COALESCE(SUM(c.estimated_minutes), 0) AS est, "
        "  COALESCE(SUM((julianday(b.ends_at) - julianday(b.starts_at)) * 1440), 0) AS actual "
        "FROM plan_block b "
        "JOIN commitment c ON c.id = b.commitment_id "
        "WHERE b.outcome = 'done' AND c.estimated_minutes IS NOT NULL",
    ).fetchone()
    return RatioReport(
        sample=int(row["n"] or 0),
        estimated_minutes=int(row["est"] or 0),
        actual_minutes=int(row["actual"] or 0),
    )
