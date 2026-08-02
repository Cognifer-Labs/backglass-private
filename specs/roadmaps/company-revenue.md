---
id: company-revenue
version: 2
title: Turn a registered company into a revenue business
horizon: annual
definition_of_done: Revenue covers infrastructure and the company files a real tax return
---

# Company revenue path

For an entity that legally exists — registered, banked, with products under it —
but has never invoiced anyone. Deliberately pitched at the ENTITY level so it
does not double-count the individual products' own roadmaps: this arc is about
the business around them.

Version 2 grounded the details in acquisition and hygiene evidence: warm network
beats cold outreach 3-5x on close rate, a channel is proven by a bounded pilot rather
than a feeling, and the tax calendar carries real per-month penalties.

## Steps

```json
{
  "steps": [
    {
      "key": "offer",
      "title": "One sentence naming what the company sells and to whom",
      "offset_weeks": 1,
      "detail": "A company with three products and no offer has three hobbies."
    },
    {
      "key": "money_in",
      "title": "Invoicing, banking, and a books rhythm working end to end",
      "offset_weeks": 3,
      "detail": "Test it with a real dollar before a client is waiting on it. Start the books cadence now — monthly reconciliation is the accepted minimum, and it is painless only while volume is small.",
      "cadences": ["books"]
    },
    {
      "key": "first_invoice",
      "title": "First paid client invoice",
      "offset_weeks": 10,
      "detail": "Warm network first: referred leads close 3-5x better than cold and 38% faster. Ten to twenty targeted messages beat a hundred sprayed ones — cold email replies run 3-5% and close ~1 deal per several hundred sends. The step that converts an LLC into a business.",
      "cadences": ["outreach"]
    },
    {
      "key": "covers_costs",
      "title": "Monthly revenue covers monthly infrastructure",
      "offset_weeks": 26,
      "detail": "The floor that makes the company self-sustaining rather than subsidised. Calibration: indie businesses that reach $1K/month typically take 8-9 months; roughly a third never do. Services monetize faster than products, lumpier."
    },
    {
      "key": "repeatable",
      "title": "Second and third clients from a repeatable channel",
      "offset_weeks": 39,
      "detail": "One client is luck; a channel is a business. Prove it with a bounded pilot: ~30 named accounts in one segment over two weeks — 5-10 positive replies validates the message, fewer means fix the segment or offer before spending more. Directionally, ~10 similar clients is when a pattern is real."
    },
    {
      "key": "clean_books",
      "title": "Books clean and the first tax return filed",
      "offset_weeks": 52,
      "detail": "Owner-of-record obligations do not pause for a course load. Schedule C rides the April 15 return (extension moves filing, never payment); an S-corp files 1120-S by March 15 with a ~$250 per shareholder per month late penalty; estimated taxes fall due April, June, September, January."
    }
  ],
  "cadences": [
    {
      "key": "outreach",
      "title": "Client conversations",
      "weekly_count": 2,
      "estimated_minutes_each": 45
    },
    {
      "key": "books",
      "title": "Books session: categorize and reconcile",
      "weekly_count": 1,
      "estimated_minutes_each": 45
    }
  ]
}
```

## Evidence notes

- Warm-vs-cold gap: referred leads close 3-5x cold, ~8x cheaper per acquisition;
  cold email benchmarks (2025, B2B): ~3.4% average reply, ~0.2% close.
- Revenue-ramp calibration is Indie Hackers self-reported cohort data — directional,
  not audited: 25th percentile 5 months to $1K/month, median 8-9, ~30% never arrive,
  half of those who do plateau under $10K.
- First-dollar-in-7-days claims are creator content; two to four weeks through warm
  network is the honest planning floor.
- Tax figures are 2026-filing-season US federal; state obligations ride on top.

## Version history

- **v2** — warm-first acquisition evidence in `first_invoice`, bounded-pilot proof
  standard in `repeatable`, revenue-ramp calibration in `covers_costs`, tax calendar
  with penalties in `clean_books`, new weekly `books` cadence.
- **v1** — initial arc.
