---
id: founder
version: 1
title: Start a company and raise a seed round
horizon: annual
definition_of_done: A funded company with a shipped product and paying or retained users.
---

# Founder path

The default annual roadmap for someone going from an idea to a seed round. Offsets assume
a full-time founder; the interview pass redates them for anyone still employed.

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
      "offset_weeks": 4,
      "detail": "Talking to twenty people is cheaper than building the wrong thing once.",
      "cadences": ["customer_conversations"]
    },
    {
      "key": "mvp",
      "title": "Ship an MVP a stranger can use unaided",
      "offset_weeks": 14
    },
    {
      "key": "first_users",
      "title": "Ten users who come back without being asked",
      "offset_weeks": 24,
      "detail": "Retention, not signups. Signups are a vanity number at this stage."
    },
    {
      "key": "traction",
      "title": "A retention or revenue chart worth showing",
      "offset_weeks": 34,
      "detail": "The deck is downstream of this. Do not build the deck first."
    },
    {
      "key": "seed",
      "title": "Close a seed round",
      "offset_weeks": 44,
      "cadences": ["investor_conversations"]
    },
    {
      "key": "scale",
      "title": "First two hires and a repeatable acquisition channel",
      "offset_weeks": 52
    }
  ],
  "cadences": [
    {
      "key": "customer_conversations",
      "title": "Customer conversations",
      "weekly_count": 3,
      "estimated_minutes_each": 45
    },
    {
      "key": "investor_conversations",
      "title": "Investor conversations",
      "weekly_count": 5,
      "estimated_minutes_each": 40
    }
  ]
}
```
