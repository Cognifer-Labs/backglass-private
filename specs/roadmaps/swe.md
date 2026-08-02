---
id: swe
version: 2
title: Land a software engineering offer
horizon: annual
definition_of_done: A signed offer for a software engineering role.
---

# Software engineering job search

Offsets are a two-quarter search with a preparation ramp in front of it. The two cadences
are the whole engine: applications create pipeline, practice sessions convert it.

Version 2 recalibrated to the 2025-2026 market: median time-to-offer runs ~15 weeks of
active search, generic applications convert at 2-3% while tailored ones hit 10-20%, and
referrals carry a 4-10x multiplier — so the cadence got smaller and heavier, and referrals
moved from a suggestion to the strategy. Most hiring lands March-June; aim the prep ramp
to be pipeline-ready by Q1.

## Steps

```json
{
  "steps": [
    {
      "key": "resume",
      "title": "Resume rewritten around shipped impact",
      "offset_weeks": 0,
      "detail": "Every bullet names a system, a change, and a measured effect. One base version, tailored per application — tailoring is the highest-leverage lever after referrals."
    },
    {
      "key": "projects",
      "title": "Two portfolio projects deployed and documented",
      "offset_weeks": 6,
      "detail": "Deployed and reachable; each defensible in twenty minutes of conversation. A private repo is not a portfolio, and a tutorial clone is not a project. LLM-integration and infra work currently draws 3-5x the callbacks of anything else."
    },
    {
      "key": "practice_base",
      "title": "Comfortable with the core data structures and patterns",
      "offset_weeks": 12,
      "detail": "Blind 75 first for pattern coverage, NeetCode 150 if runway allows. Pattern mastery beats problem count. Practice both modes: solo no-AI (still two-thirds of companies) and AI-assisted pairing (Meta, Canva, Shopify now run it) — the judged skill there is what you accept and reject, not what you type.",
      "cadences": ["leetcode_sessions"]
    },
    {
      "key": "applications",
      "title": "Application pipeline running",
      "offset_weeks": 14,
      "detail": "Referrals first — they interview at 4-10x cold-application rates and produce 30-50% of all hires from ~7% of applicants. Then careers pages, then aggregators. Fewer, tailored applications beat volume: budget 100-300 total across the search, not because more is better but because 10-40 applications per interview is the going rate.",
      "cadences": ["applications"]
    },
    {
      "key": "assessments",
      "title": "First online assessments cleared",
      "offset_weeks": 20,
      "detail": "OA bars are typically percentile-relative (~75th against the same assessment's pool) with partial credit from hidden tests — finish something on every problem.",
      "cadences": ["leetcode_sessions"]
    },
    {
      "key": "onsites",
      "title": "Onsite and system design loops",
      "offset_weeks": 28,
      "detail": "System design now shows up below senior, and trade-off reasoning is the heaviest-weighted axis. 8-12 structured mocks: front-load a few to find gaps, back-load the rest in the final two weeks. Ask each company whether its coding rounds allow AI — formats differ mid-loop now.",
      "cadences": ["mock_interviews"]
    },
    {
      "key": "offers",
      "title": "Offer in hand and negotiated",
      "offset_weeks": 36,
      "detail": "Negotiate: 87% of tech companies expect it, and the average gain is 10-20%. Bands: 5-8% ask with no leverage, 15-20%+ with competing offers or hot skills. Parallel timelines are the leverage — compress the pipeline so offers land together."
    }
  ],
  "cadences": [
    {
      "key": "applications",
      "title": "Tailored applications sent",
      "weekly_count": 8,
      "estimated_minutes_each": 30
    },
    {
      "key": "leetcode_sessions",
      "title": "Practice problem sessions",
      "weekly_count": 4,
      "estimated_minutes_each": 60
    },
    {
      "key": "mock_interviews",
      "title": "Mock interviews (coding and system design)",
      "weekly_count": 2,
      "estimated_minutes_each": 60
    }
  ]
}
```

## Evidence notes

- Conversion funnel (2025-2026): application→interview 2-3% generic, 10-20% tailored;
  interview processes end in offer ~39% — a 12-year low. Median time-to-offer ~108
  days of active search; a well-executed search has closed in 2 months (150 apps,
  5 offers), so the aggregate is a planning number, not a ceiling.
- Referral shape: 30-50% of hires from ~7% of applicants is the consensus; the
  outlier 10%-of-offers figure uses stricter counting.
- Applications cadence 10×20min → 8×30min: the research is unambiguous that the
  extra ten minutes of tailoring is worth more than the extra two sends.
- AI-assisted interviewing is real but minority (~one-third allow it); prep both
  modes rather than betting on either.

## Version history

- **v2** — tailoring-over-volume cadence (10×20 → 8×30), referral numbers inline,
  AI-era interview format guidance, OA percentile mechanics, mock cadence added,
  negotiation bands, March-June hiring-window note.
- **v1** — initial arc.
