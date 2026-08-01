---
id: company-revenue
version: 1
title: Turn a registered company into a revenue business
horizon: annual
definition_of_done: Revenue covers infrastructure and the company files a real tax return
---

# Company revenue path

For an entity that legally exists — registered, banked, with products under it —
but has never invoiced anyone. Deliberately pitched at the ENTITY level so it
does not double-count the individual products' own roadmaps: this arc is about
the business around them.

## Steps

```json
{
  "steps": [
    {"key": "offer", "title": "One sentence naming what the company sells and to whom", "offset_weeks": 1,
     "detail": "A company with three products and no offer has three hobbies."},
    {"key": "money_in", "title": "Invoicing and business banking working end to end", "offset_weeks": 3,
     "detail": "Test it with a real dollar before a client is waiting on it."},
    {"key": "first_invoice", "title": "First paid client invoice", "offset_weeks": 10,
     "detail": "The step that converts an LLC into a business.",
     "cadences": ["outreach"]},
    {"key": "covers_costs", "title": "Monthly revenue covers monthly infrastructure", "offset_weeks": 26,
     "detail": "The floor that makes the company self-sustaining rather than subsidised."},
    {"key": "repeatable", "title": "Second and third clients from a repeatable channel", "offset_weeks": 39,
     "detail": "One client is luck; a channel is a business."},
    {"key": "clean_books", "title": "Books clean and the first tax return filed", "offset_weeks": 52,
     "detail": "Owner-of-record obligations do not pause for a course load."}
  ],
  "cadences": [
    {"key": "outreach", "title": "Client conversations", "weekly_count": 2,
     "estimated_minutes_each": 45}
  ]
}
```
