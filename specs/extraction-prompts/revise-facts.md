---
id: revise-facts
version: 2
model: careful
output: strict JSON
---

# Standing facts the record has moved past

2026-08-27, the owner: *"i no longer have trayfavors, the app should know this, why does it
not process things like these"*.

It knew, twice, and could not act on either. `fact` 59 said "Joined Tray Favors team
December 2025", active, extracted on 2026-08-23 from the email in which the owner asked to
*leave* Tray Favors — the poison gate did its job, the sentence was quoted verbatim, the
confidence cleared 0.8, and the fact was true when it was written. `fact` 86 said the
placement-change reply was "PREPARED … NOT YET SENT"; it was written at 10:52 and the reply
went out at 10:58, and three answers from the coordinator arrived that afternoon.

An obligation the record has overtaken has three mechanisms. A fact had none. This pass is
the missing one: it is handed the owner's standing facts and the newest things the ledger
has read, and asked one question per fact — **has something since made this untrue?**

## What makes this different from check-relevance

That pass may drop an obligation on its own above a threshold, because a wrong drop costs
one row the owner can see is missing and re-add.

**This one may never write anything active, at any confidence.** A fact rides
`facts.owner_context` into every model call the product makes, so a wrong rewrite is not
one bad row — it is a bad premise under every triage, extraction and plan that follows,
and it compounds silently. Every verdict here lands as an ordinary `proposed` fact on the
Memory page, through the same door `facts.apply_extracted` already writes and the owner
already clicks. The asymmetry is inverted on purpose and the code enforces it.

## The rule that matters most

**An `overtaken` verdict must quote the sentence that overtook it.** Not "this looks old",
not "the situation has probably moved" — the id of a specific source item from the batch
below and words copied verbatim from it. A verdict that cannot do both is discarded rather
than repaired, exactly as in `check-relevance` and `recheck-commitments`.

Silence is not evidence. "Nobody has mentioned it lately" is `staleness`'s business and it
asks rather than acts. What this pass acts on is a positive statement in the record that
the fact can no longer be true beside: the request was sent, the placement changed, the
course was dropped, the job ended.

## Facts are not obligations, and most of them are permanent

A fact recording what happened does not stop being true because time passed. "Joined the
team in December 2025" is a permanent statement about December 2025; what became untrue is
the standing state it implied. So a fact is only `overtaken` when the record contradicts
what it asserts **now**, and the replacement must keep the history rather than delete it.

The placeholders sit at the end of the block for the same cost reason as check-relevance:
`Prompt.split` caches everything before the first one, and the rules are identical on every
call while the facts and the evidence are not.

## Prompt

```
You are checking whether what the ledger records about the user is still true, given
what the ledger has read since each of those things was recorded.

You will be given, in this order: the user's standing facts, each with an id, when it
was recorded and the evidence it was recorded from; then the newest items the ledger
has read, each with an id, a date, an author and its text.

For EACH fact return exactly one verdict:

  overtaken  something in the items says this is no longer true — the state it
             describes has changed, ended, been sent, been replaced, been cancelled
  current    anything else

Rules:

1. An `overtaken` verdict MUST name `cites_item`, the id of the item that overtook
   it, and `quote`, words copied verbatim from that item. No citation, no verdict —
   return `current` instead.

2. Silence is NOT evidence. A fact nobody has mentioned in months is not overtaken.
   Only a positive statement in the items counts.

3. Age is NOT evidence. A fact recorded long ago is usually still true.

4. A fact about something that HAPPENED stays true forever. "Joined the team in
   December 2025" is a permanent statement about December 2025. What can be overtaken
   is the standing state it implies — that the user is still on that team. When you
   return `overtaken` for one of these, the `replacement` must keep the history and
   correct the state: say that they joined then AND that it has since ended.

5. `replacement` is required on `overtaken`: what the fact should say instead, written
   as a complete statement that could stand alone in the ledger. Do not write "no
   longer true" — write what is true now, including what the old value got right.

6. An intention is not an event. "Plans to ask for a transfer" is not overtaken by
   the transfer being requested unless the fact asserted the request had not happened
   yet. Read what the fact actually claims.

7. A fact that says something is UNSENT, PENDING, DRAFTED, SCHEDULED or NOT YET DONE
   is overtaken the moment the items show it sent, answered, done or cancelled. These
   are the ones that go stale fastest and they are the reason this pass exists.

   An item marked `YOU (the user wrote this)` was written and sent BY the user. If a
   fact says a message is drafted or unsent and one of those items IS that message, the
   fact is overtaken — quote the item. A reply from the other party to a message the
   fact calls unsent is the same proof, arriving from the other end.

8. When unsure, `current` with lower confidence. Nothing here is written without the
   user's click, so a missed revision costs one more day of a stale line; a wrong one
   spends the user's attention on a question that should not have been asked.

confidence is your certainty in the verdict, 0.0 to 1.0.

The user's standing facts (judge these ids, and only these):
{{facts}}

What the ledger has read since — cite these ids, and only these:
{{items}}
```

## Version 2: say who wrote it (2026-08-27, same day)

The first live run over the real Banner thread returned `current` for the fact that said
the reply was "drafted in Mail but NOT YET SENT" — while two of the five items in front of
it were that reply, sent, and a third was the coordinator answering it. The render gave the
author as a bare address, so the judgement needed three inferential steps ("this address is
the user; therefore the user sent it; therefore it is not a draft") to reach something the
ledger knows for certain. `render` now marks the user's own items `YOU`, and rule 7 says
what that means. The static half changed, so this is a version bump rather than a silent
edit.

## Fixtures

`tests/fixtures/revise/` — the negatives carry the weight, as everywhere else.

| fixture | shape | expected |
|---|---|---|
| `01-placement-left.json` | fact says on Tray Favors; item is the owner asking to leave and the coordinator arranging the move | `overtaken`, cites the item, replacement keeps December 2025 |
| `02-unsent-now-sent.json` | fact says a reply is drafted and unsent; item is the sent reply | `overtaken` — rule 7 |
| `03-silence.json` | fact nobody has mentioned in four months, items unrelated | `current` — rule 2 |
| `04-uncited.json` | `overtaken` with no item id | discarded in code, never proposed |
| `05-history.json` | "Graduated BASIS in May 2026", items about ASU | `current` — rule 4, it happened |
