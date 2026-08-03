-- Engagements: the plans people make with each other.
--
-- The ledger has had exactly one typed record with a person on the other end — the
-- commitment, something owed in one direction or the other. That does not describe
-- dinner with a friend on Friday, a conference in March, or an interview on Tuesday.
-- Those are not owed to anyone; they are arrangements to be somewhere with someone, and
-- until now the pipeline had nowhere to put them. Worse, extract/rules.py drops calendar
-- invites at tier 0 as "owned by the calendar connector", so on an account with no
-- calendar connected the invitation was read by nothing at all.
--
-- Why a separate table and not a commitment with a flag: the questions are different.
-- A commitment asks "what do I still owe, and when is it late". An engagement asks "who
-- am I seeing, when, and have I answered them yet". They sort differently (an engagement
-- is anchored to a start time, a commitment to a deadline), they close differently (an
-- engagement happens or is declined; a commitment is discharged), and only one of them
-- belongs on a day plan as a fixed block. Folding them together would mean a `status`
-- column with two disjoint vocabularies and a `due_at` that means two things.
--
-- `kind` is social|professional. The split is the one the owner asked for and it earns
-- its place: it is what lets the brief say "you have three professional commitments and
-- no time with anyone you like this week", which is a fact about a life, not a calendar.
--
-- `starts_at` is nullable on purpose. "We should get dinner sometime" is a real plan with
-- a real person and no time — dropping it because it has no timestamp would lose exactly
-- the plans that go stale unanswered. `when_is_explicit` records whether the time came
-- from the message or from date resolution, mirroring commitment.due_is_explicit's role.
--
-- Status is a lifecycle, not a label: proposed → confirmed → done, or → declined. A plan
-- starts proposed because someone suggesting dinner is not dinner. Only `confirmed` earns
-- a block on the day plan; `proposed` is something the owner still owes an answer to,
-- which is the state the brief surfaces.

CREATE TABLE engagement (
  id               INTEGER PRIMARY KEY,
  user_id          INTEGER NOT NULL DEFAULT 1,
  kind             TEXT    NOT NULL,                      -- social|professional
  what             TEXT    NOT NULL,
  starts_at        TEXT,                                  -- NULL = agreed in principle, no time yet
  ends_at          TEXT,
  when_is_explicit INTEGER NOT NULL DEFAULT 0,
  location         TEXT,
  status           TEXT    NOT NULL DEFAULT 'proposed',   -- proposed|confirmed|declined|done|superseded
  confidence       REAL    NOT NULL,
  source_item_id   INTEGER NOT NULL REFERENCES source_item(id),
  superseded_by    INTEGER REFERENCES engagement(id),
  created_at       TEXT    NOT NULL,
  resolved_at      TEXT
);

CREATE INDEX idx_engagement_upcoming ON engagement(user_id, status, starts_at)
  WHERE status IN ('proposed', 'confirmed');

-- Many people per plan, which is the whole difference from a commitment's single
-- counterparty: "drinks with Priya and Sam" is one engagement, not two, and asking
-- "when did I last see Sam" has to find it through either name.
CREATE TABLE engagement_person (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL DEFAULT 1,
  engagement_id INTEGER NOT NULL REFERENCES engagement(id) ON DELETE CASCADE,
  entity_id     INTEGER NOT NULL REFERENCES entity(id),
  UNIQUE (user_id, engagement_id, entity_id)
);

CREATE INDEX idx_engagement_person_by_entity
  ON engagement_person(user_id, entity_id, engagement_id);

-- Provenance, on the same terms as commitment_evidence and for a sharper reason: a plan
-- is *made* across several messages. "Dinner Friday?" then "Friday works" then "see you
-- at 7" are three sightings of one engagement, and they are how a proposal becomes
-- confirmed. Throwing away the later two would discard the evidence for the status the
-- row ends up in.
--
-- Deliberately a second table rather than generalising commitment_evidence into a
-- polymorphic one: a (record_type, record_id) pair cannot be a foreign key, and giving
-- up referential integrity on the provenance table would trade the one rule the project
-- calls load-bearing for the saving of a CREATE TABLE.
--
-- UNIQUE (user_id, engagement_id, source_item_id) is what keeps rule 3 true: re-reading
-- a message the ledger has already seen conflicts instead of writing.
CREATE TABLE engagement_evidence (
  id             INTEGER PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  engagement_id  INTEGER NOT NULL REFERENCES engagement(id),
  source_item_id INTEGER NOT NULL REFERENCES source_item(id),
  quote          TEXT,                                    -- verbatim; NULL = document only
  kind           TEXT    NOT NULL DEFAULT 'original',     -- original|restated|manual
  seen_at        TEXT    NOT NULL,
  UNIQUE (user_id, engagement_id, source_item_id)
);

CREATE INDEX idx_engagement_evidence ON engagement_evidence(user_id, engagement_id, id);
CREATE INDEX idx_engagement_evidence_by_source
  ON engagement_evidence(user_id, source_item_id);
