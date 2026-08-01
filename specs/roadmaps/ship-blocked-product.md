---
id: ship-blocked-product
version: 1
title: Take a built product through its owner-only unblocks to v1.0
horizon: annual
definition_of_done: Publicly available, monetized, and legally clear — no step left that only the owner can do
---

# Blocked-product path

For a product whose engineering is finished and whose remaining work is
accounts, signatures, and policy — the failure mode is not difficulty, it is
that nobody but the owner can do any of it, so it stalls forever.

Ordered by kill risk, not by convenience: the checks that could invalidate the
business model come first, because doing KYC for a model that policy forbids is
the most expensive possible ordering.

## Steps

```json
{
  "steps": [
    {"key": "policy_check", "title": "Confirm the platform's policy allows the monetization model", "offset_weeks": 0,
     "detail": "Could kill the model. Answer this before spending a day on anything else."},
    {"key": "legal_review", "title": "Privacy policy and terms reviewed and published", "offset_weeks": 1,
     "detail": "Payments plus user data means this is not optional."},
    {"key": "payments_kyc", "title": "Payment processor onboarding and KYC complete", "offset_weeks": 2,
     "detail": "Bank details, identity verification, tax forms — days of latency, not hours."},
    {"key": "distribution", "title": "Published to every distribution channel", "offset_weeks": 3,
     "detail": "Package registries, extension marketplaces, signed and notarized binaries."},
    {"key": "first_external_user", "title": "First user who is not the owner completes the loop", "offset_weeks": 5,
     "detail": "Installed, used, and paid or paid-out — the whole circuit, by a stranger."},
    {"key": "v1", "title": "v1.0 tagged and announced", "offset_weeks": 7,
     "detail": "Version the thing so the next change is an upgrade, not a rewrite."}
  ],
  "cadences": [
    {"key": "unblock_sessions", "title": "Unblock sessions", "weekly_count": 2,
     "estimated_minutes_each": 60}
  ]
}
```
