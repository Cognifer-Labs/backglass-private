---
id: app-launch
version: 2
title: Ship an app to the App Store and its first users
horizon: annual
signals: testflight, app store, app review, beta tester, subscription
definition_of_done: Live on the App Store with 100 active users and paying subscribers
---

# App launch path

Milestones are offsets from the roadmap start date; the interview or the owner
re-dates them. Built for a product that already exists in development — this is
the shipping-and-traction arc, not the build-from-zero arc.

Version 2 calibrated against RevenueCat's State of Subscription Apps (75K+ apps —
the only large-sample dataset in this space) and current App Review behavior. The
honest headline: median new-app revenue at twelve months is under $50/month, and only
~17-20% of new apps ever reach $1,000 — the arc keeps paying-cohort as the milestone
and treats $1K/month as the stretch beyond it.

## Steps

```json
{
  "steps": [
    {"key": "beta_build", "title": "Feature-complete beta on TestFlight", "offset_weeks": 2,
     "detail": "Cut scope until the core loop survives a stranger. Beta builds get their own light review (24-48h) and expire after 90 days — the cohort clock starts at upload."},
    {"key": "beta_cohort", "title": "15 real testers through the full loop", "offset_weeks": 5,
     "detail": "Recruited, onboarded, and interviewed — not just installed. Small-and-targeted beats large-and-generic. Structure it: native TestFlight feedback as the floor, one survey, weekly testing tasks, and close the loop when their bug ships.",
     "cadences": ["feedback_calls"]},
    {"key": "store_submission", "title": "App Store submission accepted", "offset_weeks": 8,
     "detail": "Apple reviews 90% of submissions inside 24h; budget 1-3 rejection rounds anyway. The two highest-value pre-checks: completeness (crashes, placeholder content — the top unresolved-rejection cause) and privacy labels that match actual SDK behavior — Apple cross-references, and a mismatch or dead policy link is an automatic rejection."},
    {"key": "launch", "title": "Public launch", "offset_weeks": 10,
     "detail": "Store live + launch posts. One channel done well beats five done thin. Product Hunt is a 24h visibility event, not a growth channel — half of launches see only a temporary spike; skip it unless an audience already exists. The first screenshot carries most of listing conversion."},
    {"key": "hundred_users", "title": "100 active users", "offset_weeks": 16,
     "detail": "Active, not installed. Model growth as linear this early — ASO plus word of mouth compounds in months, not weeks; nobody publishes a credible time-to-100 benchmark because there isn't one."},
    {"key": "first_revenue", "title": "First paying cohort through the paywall", "offset_weeks": 22,
     "detail": "Conversion data beats paywall theory. Medians to beat: 6.2% install→trial, hard paywall converts ~12% of downloads by day 35 vs ~2% freemium, and 17-32-day trials convert best (~46%) while 3-7-day trials churn hardest."},
    {"key": "retention_pass", "title": "v1.1 retention release from live data", "offset_weeks": 30,
     "detail": "Ship what the cohort's behavior says, not the backlog. Gate on D30 — ideally D60 — and credit a retention win only against a cohort that did not get the change. Calibration: year-one retention medians are 44% annual plans, 17% monthly; ~30% of annual subs cancel in month one.",
     "cadences": ["cohort_review"]}
  ],
  "cadences": [
    {"key": "feedback_calls", "title": "User feedback conversations",
     "weekly_count": 3, "estimated_minutes_each": 30},
    {"key": "build_blocks", "title": "Focused build sessions",
     "weekly_count": 4, "estimated_minutes_each": 90},
    {"key": "cohort_review", "title": "Weekly cohort and funnel review",
     "weekly_count": 1, "estimated_minutes_each": 30}
  ]
}
```

## Evidence notes

- Subscription benchmarks: RevenueCat State of Subscription Apps 2025/2026 (75K-115K
  apps). Dispersion is extreme — top 5% of new apps earn ~400x the bottom quartile in
  year one; among apps that do reach $1,000 revenue, median time is ~60 days.
- Rejection-rate percentages circulating online (17% first-pass, 4.3-spam breakdowns)
  trace to SEO content, not Apple — the preset encodes only the planning heuristic
  (1-3 rounds) and the two verified auto-rejection vectors.
- Product Hunt: ~10% of launches get Featured now, and Featured status drives most of
  the outcome; the signup spread across launches is explained mostly by pre-existing
  audience.

## Version history

- **v2** — RevenueCat funnel and retention medians in `first_revenue`/`retention_pass`,
  privacy-label and completeness pre-checks in `store_submission`, Product Hunt
  de-emphasized in `launch`, linear-growth framing in `hundred_users`, new weekly
  `cohort_review` cadence.
- **v1** — initial arc.
