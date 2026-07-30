---
id: swe
version: 1
title: Land a software engineering offer
horizon: annual
definition_of_done: A signed offer for a software engineering role.
---

# Software engineering job search

Offsets are a two-quarter search with a preparation ramp in front of it. The two cadences
are the whole engine: applications create pipeline, practice sessions convert it.

## Steps

```json
{
  "steps": [
    {
      "key": "resume",
      "title": "Resume rewritten around shipped impact",
      "offset_weeks": 0,
      "detail": "Every bullet names a system, a change, and a measured effect."
    },
    {
      "key": "projects",
      "title": "Two portfolio projects deployed and documented",
      "offset_weeks": 6,
      "detail": "Deployed and reachable. A private repo is not a portfolio."
    },
    {
      "key": "practice_base",
      "title": "Comfortable with the core data structures and patterns",
      "offset_weeks": 12,
      "cadences": ["leetcode_sessions"]
    },
    {
      "key": "applications",
      "title": "Application pipeline running",
      "offset_weeks": 14,
      "detail": "Referrals first, then the careers page, then aggregators.",
      "cadences": ["applications"]
    },
    {
      "key": "assessments",
      "title": "First online assessments cleared",
      "offset_weeks": 20,
      "cadences": ["leetcode_sessions"]
    },
    {
      "key": "onsites",
      "title": "Onsite and system design loops",
      "offset_weeks": 28
    },
    {
      "key": "offers",
      "title": "Offer in hand and negotiated",
      "offset_weeks": 36
    }
  ],
  "cadences": [
    {
      "key": "applications",
      "title": "Applications sent",
      "weekly_count": 10,
      "estimated_minutes_each": 20
    },
    {
      "key": "leetcode_sessions",
      "title": "Practice problem sessions",
      "weekly_count": 4,
      "estimated_minutes_each": 60
    }
  ]
}
```
