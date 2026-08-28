-- The judged-once ledger for standing facts, and the gap it closes.
--
-- 2026-08-27, the owner: *"i no longer have trayfavors, the app should know this, why does
-- it not process things like these"*. It did know, twice over, and could not act on either.
--
-- `fact` row 59 said "Joined Tray Favors team December 2025", status active, extracted on
-- 2026-08-23 from source item 10576 — the email in which the owner asked to *leave* Tray
-- Favors. The poison gate behaved exactly as designed: the sentence was quoted verbatim,
-- the confidence cleared 0.8, the lane was known, so it landed active. It was true when it
-- was written. What was missing is anything that reads it again.
--
-- `fact` row 86 said the placement-change reply was "PREPARED ... NOT YET SENT". It was
-- written at 10:52 on 2026-08-24 and the reply went out at 10:58, and three answers from
-- the coordinator arrived the same afternoon. Four days later the ledger still said the
-- mail was sitting in a draft.
--
-- An obligation the record has overtaken has three mechanisms: `logic` throws out what is
-- structurally contradicted, `extract/relevance` retires what a recorded fact makes moot,
-- `extract/recheck` re-reads a chat commitment against what the conversation said next.
-- A *fact* the record has overtaken had none. `questions._contradictions` is the closest
-- thing and it cannot fire here: it needs the same (subject, key) written twice with two
-- values, and nothing ever wrote a second one — the fact simply went stale in place.
--
-- That is worse than inert. `facts.owner_context` rides into every model call, so the
-- ledger has been telling itself the owner is on Tray Favors, with an unsent draft, on
-- every triage, extraction and plan since the 24th.
--
-- **This table is the recurrence guard, not the verdict.** The verdict lands as an
-- ordinary `proposed` fact through the path `facts.apply_extracted` already writes and
-- the Memory page already renders, because "a fact becomes current" must have exactly one
-- door and the owner's click is it. Nothing here supersedes anything by itself — see
-- `backglass/extract/revision.py` for why the asymmetry is inverted against
-- `extract/relevance`'s: a wrong obligation-drop costs one row the owner can re-add, and a
-- wrong fact rewrite poisons every model call that reads owner_context afterwards.
--
-- `through_item` is the newest `source_item` id the judgement considered. Keying on
-- (fact_id, through_item) is what makes the pass cheap and honest at once: a fact is
-- re-judged when, and only when, evidence has arrived since the last time anyone looked at
-- it. A watermark on the run would re-judge everything on every sync; a watermark on the
-- fact alone would never re-judge it at all.

CREATE TABLE fact_check (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL DEFAULT 1,
  fact_id       INTEGER NOT NULL REFERENCES fact(id),
  -- The newest source item in the evidence this verdict was formed against.
  through_item  INTEGER NOT NULL,
  verdict       TEXT    NOT NULL,           -- current|overtaken
  confidence    REAL    NOT NULL,
  -- The item whose words overtook the fact, and those words. Both required for
  -- `overtaken`: a verdict that cannot quote the sentence that changed things is
  -- discarded rather than repaired, the same rule check-relevance and recheck apply.
  cites_item    INTEGER REFERENCES source_item(id),
  quote         TEXT,
  -- What the fact should say instead, as the model read it. This becomes the proposed
  -- fact's value; it is never written active from here.
  replacement   TEXT,
  reason        TEXT,
  -- The `proposed` fact row this verdict raised, so the surface can be traced back to
  -- the judgement and a second run does not raise it twice.
  proposed_fact INTEGER REFERENCES fact(id),
  status        TEXT    NOT NULL DEFAULT 'pending', -- pending|proposed|current
  created_at    TEXT    NOT NULL,
  UNIQUE (user_id, fact_id, through_item)
);

CREATE INDEX idx_fact_check_fact ON fact_check(user_id, fact_id);
