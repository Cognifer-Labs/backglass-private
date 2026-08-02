---
id: founder
version: 2
title: Start a company and raise a seed round
horizon: annual
definition_of_done: A funded company with a shipped product and paying or retained users.
---

# Founder path

The default annual roadmap for someone going from an idea to a seed round. Offsets assume
a full-time founder; the interview pass redates them for anyone still employed.

Version 2 recalibrated against 2025-2026 fundraising data (Carta State of Seed,
PitchBook-NVCA, DocSend, CRV). The uncomfortable headline: the seed bar has risen —
investors say validation suffices, but in practice many now expect $300-500K ARR and a
working product, because AI-native companies show revenue at seed. The arc still ends at
a seed close; the details tell the truth about what that takes now.

## Steps

```json
{
  "steps": [
    {
      "key": "idea",
      "title": "Pick the problem and write it down",
      "offset_weeks": 0,
      "detail": "One page: who hurts, how much, what they do today.",
      "cadences": ["customer_conversations"]
    },
    {
      "key": "validate",
      "title": "Twenty problem interviews before writing code",
      "offset_weeks": 3,
      "detail": "Ten to twenty interviews is where patterns saturate — new conversations stop adding information. Mom Test rules: ask about their life, not your idea. Cheaper than building the wrong thing once.",
      "cadences": ["customer_conversations"]
    },
    {
      "key": "mvp",
      "title": "Ship an MVP a stranger can use unaided",
      "offset_weeks": 12,
      "detail": "Standard SaaS MVP is a 4-8 week build; AI tooling compresses simple ones to 2-4. If the build is running past eight weeks, the scope is wrong, not the timeline."
    },
    {
      "key": "first_users",
      "title": "Ten users who come back without being asked",
      "offset_weeks": 22,
      "detail": "Retention, not signups. The signal that counts is a cohort curve that flattens instead of decaying to zero — and 40%+ answering 'very disappointed' if the product vanished (Sean Ellis test)."
    },
    {
      "key": "traction",
      "title": "A retention or revenue chart worth showing",
      "offset_weeks": 32,
      "detail": "The deck is downstream of this. Today's practical seed bar: real usage with efficient economics, and increasingly $300-500K ARR. If the chart isn't there, a pre-seed on a SAFE (90% of pre-seed deals) buys 12-18 months to build it."
    },
    {
      "key": "seed",
      "title": "Close a seed round",
      "offset_weeks": 44,
      "detail": "Run it as a compressed 6-8 week parallel process, never sequentially: 200-300 name target list, ~40 meetings taken, 5-10% of meetings convert to checks. Median seed: $3-4M on $16-18M pre-money, ~20% dilution, SAFE with cap or discount, never both.",
      "cadences": ["investor_conversations"]
    },
    {
      "key": "scale",
      "title": "First two hires and a repeatable acquisition channel",
      "offset_weeks": 52,
      "detail": "Start sourcing the day the round closes — time-to-hire runs 2-3 months. Gate non-engineering hires on delegable repeatable work and 12-18 months of runway left, not on the bank balance."
    }
  ],
  "cadences": [
    {
      "key": "customer_conversations",
      "title": "Customer conversations",
      "weekly_count": 4,
      "estimated_minutes_each": 45
    },
    {
      "key": "investor_conversations",
      "title": "Investor conversations",
      "weekly_count": 7,
      "estimated_minutes_each": 40
    }
  ]
}
```

## Evidence notes

- Interview saturation at 10-20 conversations: Mom Test / YC Startup School lineage.
  YC tracks "users talked to last week" as a standing weekly metric — discovery is a
  cadence, not a phase; the cadence stays on after validation ends.
- Raise mechanics: DocSend — 37% of successful raises close in 1-6 weeks, 32% in 7-18;
  planning guidance says hold 8-12 weeks. The compressed-window strategy (Holloway,
  Hustle Fund) exists to create scarcity; a raise that drags reads as damaged goods.
- Seed sizing: Carta ($4M / $16M pre) vs PitchBook-NVCA ($3.0M / $18.4M pre) — platform
  sample bias both ways; the preset quotes the range.
- Investor cadence of 7/week reflects the active-raise sprint only; outside the raise
  window the honest number is ~1 (keeping the update list warm).

## Version history

- **v2** — recalibrated to 2025-2026 data: raised the traction bar language (AI-era
  $300-500K ARR reality, pre-seed as the fallback), added raise funnel numbers
  (200-300 list / ~40 meetings / 5-10% conversion, 6-8 week window), PMF signals
  named (curve flattening, Sean Ellis 40%), MVP window tightened 14→12, investor
  cadence 5→7 during the sprint.
- **v1** — initial arc.
