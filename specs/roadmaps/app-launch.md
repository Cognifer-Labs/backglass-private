---
id: app-launch
version: 1
title: Ship an app to the App Store and its first users
horizon: annual
definition_of_done: Live on the App Store with 100 active users and paying subscribers
---

# App launch path

Milestones are offsets from the roadmap start date; the interview or the owner
re-dates them. Built for a product that already exists in development — this is
the shipping-and-traction arc, not the build-from-zero arc.

## Steps

```json
{
  "steps": [
    {"key": "beta_build", "title": "Feature-complete beta on TestFlight", "offset_weeks": 2,
     "detail": "Cut scope until the core loop survives a stranger."},
    {"key": "beta_cohort", "title": "15 real testers through the full loop", "offset_weeks": 5,
     "detail": "Recruited, onboarded, and interviewed — not just installed.",
     "cadences": ["feedback_calls"]},
    {"key": "store_submission", "title": "App Store submission accepted", "offset_weeks": 8,
     "detail": "Screenshots, privacy labels, review notes; expect one rejection round."},
    {"key": "launch", "title": "Public launch", "offset_weeks": 10,
     "detail": "Store live + launch posts. One channel done well beats five done thin."},
    {"key": "hundred_users", "title": "100 active users", "offset_weeks": 16,
     "detail": "Active, not installed. Retention is the number under the number."},
    {"key": "first_revenue", "title": "First paying cohort through the paywall", "offset_weeks": 22,
     "detail": "Conversion data beats paywall theory."},
    {"key": "retention_pass", "title": "v1.1 retention release from live data", "offset_weeks": 30,
     "detail": "Ship what the cohort's behavior says, not the backlog."}
  ],
  "cadences": [
    {"key": "feedback_calls", "title": "User feedback conversations",
     "weekly_count": 3, "estimated_minutes_each": 30},
    {"key": "build_blocks", "title": "Focused build sessions",
     "weekly_count": 4, "estimated_minutes_each": 90}
  ]
}
```
