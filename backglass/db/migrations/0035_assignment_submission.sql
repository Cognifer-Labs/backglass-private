-- What the ICS feed cannot say: whether the work is already handed in.
--
-- `connectors/canvas_ics.py` names this as its one real downgrade, in its own docstring:
-- "The feed carries no submission state. `canvas.py` drops anything already submitted or
-- graded, and that filter is the most valuable thing the API gives; here it is absent, so
-- work already handed in keeps reading as an open obligation until the owner closes it."
--
-- Measured on the owner's ledger, 2026-08-27: **13 assignments Canvas has already graded
-- were still open commitments**, holding 434 minutes of planner capacity. One of them was
-- on that morning's plan. The board was asking for four hundred minutes of work that was
-- finished, and every day it did that it displaced work that was not.
--
-- The columns are here rather than in a new table because they are facts about the
-- assignment row that already exists, from the same institution that supplied it. Nothing
-- reads them without also reading the row.
--
-- Why the enrichment is a hand-run import and not a connector. ASU disables
-- student-generated tokens, which is the documented reason `canvas_ics.py` exists at all.
-- The REST API does answer on the browser session in a signed-in tab, and that is
-- deliberately NOT turned into a connector: `canvas_ics.py` forswears session cookies and
-- scraping in the same docstring, and a sync job that rides the owner's cookie is exactly
-- the thing it refuses. `backglass coursework --enrich <file>` takes a document the owner
-- exported from their own browser, in their own session, at a moment they chose. That is
-- an owner-in-the-loop import, and it is the only shape this may take.
--
-- Nullable throughout, and no default. NULL means "no enrichment has been applied to this
-- row", which is a different statement from "Canvas says zero" — `score = 0` on a graded
-- submission is a real and common answer, and a DEFAULT 0 would make the two unreadable.

ALTER TABLE assignment ADD COLUMN points_possible  REAL;
ALTER TABLE assignment ADD COLUMN submitted_at     TEXT;
-- Canvas's own submission workflow_state: unsubmitted|submitted|graded|pending_review.
ALTER TABLE assignment ADD COLUMN submission_state TEXT;
ALTER TABLE assignment ADD COLUMN score            REAL;
-- When an import last spoke about this row, so a stale enrichment is legible as stale
-- rather than silently trusted forever. Distinct from `last_changed_at`, which is the
-- feed's clock; this is the browser export's.
ALTER TABLE assignment ADD COLUMN enriched_at      TEXT;
