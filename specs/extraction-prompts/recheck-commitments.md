---
id: recheck-commitments
version: 1
model: careful
output: strict JSON
---

# Re-checking chat commitments against what happened next

Every other extraction pass reads forward: a message arrives, and it may create or resolve
an obligation. That works for mail, where completion is announced — "here's that deck I
owed you". It cannot work for friends. "Bring dress shoes" is answered by bringing dress
shoes; nobody writes a closing message, and the thread moves on.

Measured on the owner's ledger: 84 open commitments from `imessage` across 16
conversations, 79 with later messages in the same chat, and not one ever closed by them.

So this pass reads **backwards**. It is handed a conversation's open commitments *with
their ledger ids* and the messages that arrived after them, and asked one question per
commitment: given what was said next, is this promise still live?

Batched per conversation rather than per commitment. One call per chat per sync, on chats
that have said something new — 84 per-commitment calls at the extract tier would cost real
money and would re-send the same window once per commitment.

## The rule that matters most

**Silence is not evidence.** A verdict that closes a commitment must cite a specific
message id and quote it. If the conversation simply moved on without touching the
obligation, the verdict is `open`. "Nobody mentioned it again" is exactly the reasoning
that would close every real obligation the owner has been quietly failing to do, and it is
the one failure this pass could cause that nobody would see.

## Prompt

```
You are re-reading one conversation to decide which of the user's open promises it
has settled.

Below are the user's open commitments from this conversation, each with an id, and
then the messages that arrived afterwards, each with a message id.

For EACH commitment, return exactly one verdict:

  done     the conversation shows the thing happened
  dropped  the conversation shows it is no longer wanted or no longer possible
  open     anything else, including no mention at all

Rules:

1. A `done` or `dropped` verdict MUST cite one message id from the window and quote
   the words in it that settle the matter, verbatim. If you cannot quote it, the
   verdict is `open`. Silence is never evidence.

2. Quote only what is actually there. Do not paraphrase, complete, or infer a quote.

3. "Sounds good", "ok", "sure" and other acknowledgements settle nothing. They agree
   to a plan; they do not report that it happened.

4. Only the person who owes the thing can complete it. Somebody else in a group
   saying they did it does not close the user's obligation, unless the message makes
   clear it was done on the user's behalf and instead of it.

5. A plan that MOVED is still open. "Can we do it next week" is a new date, not a
   closure. Only say `dropped` when the conversation shows it is off — "don't worry
   about it", "I got it already", "we're not doing that any more".

6. A commitment that is about a recurring or standing arrangement is not closed by one
   instance of it happening.

7. When the evidence is partial or you are unsure, return `open` with lower
   confidence. An obligation wrongly left open is visible to the user and costs one
   click. An obligation wrongly closed disappears silently.

confidence is your certainty in the verdict, 0.0 to 1.0.

Open commitments:
{{commitments}}

Messages since these were recorded:
{{messages}}
```

## Fixtures

`tests/fixtures/recheck/` — each carries the open list, the window, and the expected
verdicts. The negatives are the point of the set:

| fixture | shape | expected |
|---|---|---|
| `01-done-cited.json` | "did you bring them" → "yeah dropped them off this morning" | `done`, cited |
| `02-still-open.json` | the window never mentions it | `open`, no citation |
| `03-acknowledgement.json` | "sounds good" and nothing else | `open` — rule 3 |
| `04-someone-else.json` | another group member says *they* did it | `open` — rule 4 |
| `05-plan-moved.json` | "can we push to next week" | `open` — rule 5, not `dropped` |
| `06-dropped-cited.json` | "don't worry about it, I picked one up" | `dropped`, cited |
| `07-before-window.json` | the closing message predates the watermark | not offered at all |

`07` is the watermark edge and is checked in `recheck.py` rather than by the model: a
message the pass never sends cannot be cited, and a citation naming an id outside the
window is discarded like any other uncited verdict.
