---
id: pm
version: 1
title: Break into product management
horizon: annual
definition_of_done: A signed offer for an APM or product manager role.
---

# Product management path

Aimed at an APM-cycle candidate. APM applications are seasonal, so the offsets front-load
fundamentals and portfolio work before the autumn window.

## Steps

```json
{
  "steps": [
    {
      "key": "fundamentals",
      "title": "Product fundamentals: metrics, prioritisation, discovery",
      "offset_weeks": 0,
      "detail": "Enough to argue about a metric definition and be right."
    },
    {
      "key": "portfolio",
      "title": "Three written product cases",
      "offset_weeks": 8,
      "detail": "A teardown, a spec for something you shipped, and a strategy memo."
    },
    {
      "key": "networking",
      "title": "Twenty informational conversations with working PMs",
      "offset_weeks": 14,
      "cadences": ["informational_chats"]
    },
    {
      "key": "apm_applications",
      "title": "APM applications submitted",
      "offset_weeks": 22,
      "detail": "APM programmes open and close in weeks. Missing the window costs a year.",
      "cadences": ["applications"]
    },
    {
      "key": "case_practice",
      "title": "Mock product and analytical interviews",
      "offset_weeks": 26,
      "cadences": ["mock_interviews"]
    },
    {
      "key": "interviews",
      "title": "Final round loops",
      "offset_weeks": 34
    },
    {
      "key": "offer",
      "title": "Offer accepted",
      "offset_weeks": 42
    }
  ],
  "cadences": [
    {
      "key": "informational_chats",
      "title": "Informational conversations",
      "weekly_count": 2,
      "estimated_minutes_each": 30
    },
    {
      "key": "applications",
      "title": "Applications sent",
      "weekly_count": 5,
      "estimated_minutes_each": 45
    },
    {
      "key": "mock_interviews",
      "title": "Mock interviews",
      "weekly_count": 2,
      "estimated_minutes_each": 60
    }
  ]
}
```
