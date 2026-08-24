# What is actually connected across sources — measured, 2026-08-24

Owner: *"how do we connect and make logic better across and within sources"*

Read-only over the live ledger. Every number names its query.

## The headline

Eleven sources, 10,563 items, and the ledger almost never joins two of them.

| question | query | answer |
|---|---|---|
| commitments with evidence from more than one source | `commitment_evidence` ⋈ `source_item`, `HAVING COUNT(DISTINCT source) > 1` | **3** of 386 open |
| people whose evidence spans more than one source | same shape over `commitment.counterparty_entity_id` | **9** of 127 |
| rows in `touchpoint` | `SELECT COUNT(*)` | **1**, and its source is `manual` |
| people carrying both an email and a phone alias | `aliases_json LIKE '%@%' AND GLOB '*+1*'` | **5** of 237 |
| has the contacts connector ever run | `source_item WHERE source LIKE '%contact%'` | **0** items; credential row says `ok` |

The sources themselves are healthy — apple-mail 5,261, imessage 4,554, calendar 317,
canvas 160. The evidence is all there. Nothing is joining it.

## Why, mechanically

**1. Identity is the join key, and the two loudest sources speak different languages.**
Mail identifies people by address, iMessage by phone number. `resolve_entity` matches on
what the item carries, so the same person arrives as two entities and neither knows about
the other. Five people out of 237 carry both kinds of alias. `apple-contacts` is
configured, its credential reads `ok`, and it has produced zero items — Contacts.app is
holding exactly the address↔number pairs that would bridge this, and nothing has asked it.

**2. `touchpoint` is empty, so the people tier of every model call is blank.**
`people/touch.py:217` is the only writer and it is reached only from `backglass people
touch`, typed by hand. No connector, no extraction pass, and no loop pass records that a
person was heard from. So `context._people` — one of the three tiers the goal set on
2026-08-18 exists to build — has nothing to read, `entity.updated_at` has no evidence
behind it, and "who is the owner currently entangled with" cannot be answered from the
ledger even though 9,800 mail and message items know.

**3. The logic checker reasons within a row, not across sources.**
Of its rules, `_reported_done` is a regex on one commitment's own text, `_events_whose_day_has_passed`
and `_canvas_assignments_past_grace` are date comparisons on one table.
`_questions_the_calendar_no_longer_supports` is the only one that reads a second source to
judge the first, and it is the shape the others should follow: it closes a question because
*a different source stopped agreeing with it*.

The boundary in `logic.py`'s docstring — positive contradiction, never silence — is right
and is not what limits this. A contradiction between two sources is still a positive
contradiction. There simply are not any, because nothing links the rows that would
disagree.

## The order these would go in

1. **Run the contacts connector.** It is configured, it is one command, and it is what
   turns two half-people into one. Every join below gets better the moment it lands, and
   nothing else on this list is worth building before it.
2. **Write a touchpoint from ingest.** Every kept item that resolves to an entity is a
   touch. Deterministic, no model, no new table, and it fills the people tier that three
   surfaces already read.
3. **Then cross-source rules become possible**, in `logic.py`'s existing shape: a
   commitment from mail that a message answers; a Canvas deadline the calendar retracted;
   an engagement two sources describe at the same hour. Each one is still a positive
   contradiction, and each one needs (1) and (2) to have anything to stand on.

Not proposed: a similarity join over item text. CLAUDE.md's boundary holds — retrieval
finds documents, extraction states facts, and a cross-source *belief* built on a score
would make the additive layer load-bearing.
