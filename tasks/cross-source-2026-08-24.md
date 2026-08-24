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
| people carrying a phone alias | `aliases_json GLOB '*+1*'` | 66 of 237 |
| people carrying an email alias | `aliases_json LIKE '%@%'` | 13 of 237 |
| people carrying **both** — the bridge | both of the above | **5** of 237 |

The sources themselves are healthy — apple-mail 5,261, imessage 4,554, calendar 317,
canvas 160. The evidence is all there. Nothing is joining it.

## Why, mechanically

**1. Identity is the join key, and the two loudest sources speak different languages.**
Mail identifies people by address, iMessage by phone number. `resolve_entity` matches on
what the item carries, so the same person arrives as two entities and neither knows the
other exists.

**Correction, and it changes the remedy.** The first version of this section said the
contacts connector had never run, on the evidence of `source_item WHERE source LIKE
'%contact%'` → 0. That probe was wrong: contacts does not write source items, it writes
aliases onto `entity`, and `_contacts_pass` is wired into every sync. Zero is what that
query returns whether or not the connector has ever run — the same shape as this repo's
2026-08-23 lesson about a verification whose identifiers are wrong passing on an empty
set, made again on the same day.

It has run. `APPLE_CONTACTS=1` is set and 66 people carry a phone alias. What it did not
do is bridge: only 13 people carry an email alias at all, and **5 carry both**. So the
address book supplied numbers for people the ledger mostly knows *by* number already, and
the mail side of the same person stayed separate. The bridge is not missing because a
command was never typed — it is missing because the two populations barely overlap.

That makes the real question narrower and more answerable: for the handful of people who
matter (the ~127 with an open commitment), which are present in both mail and messages
under different identifiers, and is the missing link in Contacts.app at all or does it
need the owner? `contacts.py` already refuses to guess when two cards claim one number
(`report.ambiguous`) — that refusal is correct, and it is also a list nobody reads.

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

1. **Measure the bridge before widening it.** Contacts already runs. For the 127 people
   carrying an open commitment, count how many appear in both mail and messages under
   different identifiers, and how many of those Contacts.app could reconcile if asked.
   That number decides whether this is a connector problem, a matching problem, or a
   handful of rows the owner should merge by hand on `/people`. It is a query, not a
   build, and every remedy below depends on which of the three it is.
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
