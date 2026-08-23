# Morning Brief

Delivered 06:00 local by email. Read in under two minutes or it has failed.

## Sections, in order

Sections vanish entirely when empty. They never render an empty header, because a brief
that is mostly empty headers trains you to stop opening it.

1. **Timezone change** — only when today's active zone differs from yesterday's. First,
   above everything. Shows the working window in both zones.
2. **Today's plan** — proposed blocks from the day planner, protected block marked,
   plus any routine the day left no room for (docs/04 §1.9, P18).
3. **Capacity line** — one sentence. "6h 15m available, 5h 30m planned, 3 items did not
   fit."
4. **Slipping** — due within 48 hours with no visible progress.
5. **Awaiting others** — owed to you, with age.
6. **Goal pulse** — active goals, target progress, staleness or risk where flagged.
7. **Rollover** — items rolling over a third time, with the drop-or-do question.
8. **Needs review** — low-confidence extractions, accept or reject links.

Monday replaces the whole brief with weekly planning. Friday appends the retro. Both are
specified in `docs/04-daily-schedule-and-goals.md` §2.7.

## Hard requirements

| ID | Requirement |
|----|-------------|
| B1 | Under 400 words. Enforced in code; truncate the lowest-priority section and say so. |
| B2 | Every line links to its source. A line with no provenance does not render. |
| B3 | Empty sections are omitted, not shown empty. |
| B4 | No item appears in two sections. Precedence is the section order above. |
| B5 | Generated from ledger state only. No live API calls at send time. |
| B6 | If generation fails, send a short failure notice rather than nothing. Silence is indistinguishable from "no news" and that ambiguity is corrosive. |
| B7 | Record `brief.opened_at` via tracking pixel or link click, and use it. A brief nobody opens is the signal that matters most. |

## Email constraints

The brief is email, and email is where the cream-and-black scheme pays off. It survives
every client, forced inversion, and stripped stylesheet, because nothing depends on a
subtle hue relationship.

- Inline every style. No `<style>` block, no CSS variables, no external stylesheet.
- Table-based layout. Flexbox and grid are not reliable.
- Set the background cream explicitly on the outer table, not just on `body`.
- Use black rules for section breaks rather than colored headers.
- Colors: black, cream, and the five inks from `design/design-system.md`. Every ink fill
  gets a black keyline, same as the dashboard.
- Test in Gmail web, Gmail iOS, Apple Mail, and Outlook before shipping. They will
  disagree.

## Tone

Declarative and short. No greeting, no sign-off, no encouragement.

Good: "Migration plan due to NY DOH, 11:30. Committed 14 Jul."

Bad: "Good morning! You've got a busy day ahead — let's make it a great one. First up..."

The brief is an instrument reading, not a coach. Every word of padding costs attention
against a 400-word ceiling.

## Failure states worth rendering

- A source has been failing for more than one cycle: name it at the top. "Gmail auth
  expired 3 days ago, this brief is incomplete."
- Spend cap reached and pipeline degraded to triage-only: say so.
- No plan could be generated because the day is fully booked: say that rather than
  showing an empty plan.

Each of these is more valuable than any section it displaces.
