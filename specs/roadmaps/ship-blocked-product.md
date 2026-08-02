---
id: ship-blocked-product
version: 2
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
the most expensive possible ordering. Version 2 added the latency evidence — the
two long-tail items (payment onboarding, distribution accounts) start in week one
and run in parallel, because their tails are measured in weeks, not hours.

## Steps

```json
{
  "steps": [
    {"key": "policy_check", "title": "Confirm the platform's policy allows the monetization model", "offset_weeks": 0,
     "detail": "Could kill the model — answer before spending a day on anything else. Real examples: VS Code Marketplace has no paid path at all (license-key gating only); Chrome Web Store bans remotely-hosted code outright and restricts affiliate monetization; App Store external-payment-link fee terms are in active legal flux — verify current terms, don't assume."},
    {"key": "payments_kyc", "title": "Payment processor onboarding and KYC complete", "offset_weeks": 1,
     "detail": "Start week one: identity docs clear in days when clean but stall indefinitely on mismatches, and the first payout carries a fixed 7-14 day risk hold regardless. Bank details, tax forms, beneficial owners — latency floor, not paperwork."},
    {"key": "legal_review", "title": "Privacy policy and terms reviewed and published", "offset_weeks": 2,
     "detail": "Payments plus user data means this is not optional. A generator (Termly, iubenda) is adequate for straightforward data practices; lawyer only for health/finance/children's data. Both app stores hard-reject a broken policy link, and Apple auto-rejects privacy labels that don't match actual SDK behavior."},
    {"key": "distribution", "title": "Published to every distribution channel", "offset_weeks": 4,
     "detail": "Package registries, extension marketplaces, signed and notarized binaries. Latency map: npm/PyPI instant (npm now wants trusted publishing — classic tokens are dead), notarization usually minutes with a documented tail of days, Chrome Web Store 2-7 days (3 weeks if manual review), Apple org enrollment via D-U-N-S can run weeks — if one is needed, open it in week one."},
    {"key": "first_external_user", "title": "First user who is not the owner completes the loop", "offset_weeks": 6,
     "detail": "Installed, used, and paid or paid-out — the whole circuit, by a stranger. The payout half can't clear before the processor's first-payout hold does; sequence accordingly."},
    {"key": "v1", "title": "v1.0 tagged and announced", "offset_weeks": 8,
     "detail": "Version the thing so the next change is an upgrade, not a rewrite."}
  ],
  "cadences": [
    {"key": "unblock_sessions", "title": "Unblock sessions", "weekly_count": 2,
     "estimated_minutes_each": 60}
  ]
}
```

## Evidence notes

- The policy-first ordering is load-bearing, not tidiness: a dev can clear Stripe KYC
  and land paying users only to find the distribution channel forbids the model
  (VS Code Marketplace ships free-only; Chrome's remote-code ban kills the
  thin-extension-fat-server architecture; App Store US anti-steering terms have
  changed multiple times since 2021 and are under appeal).
- Stripe first-payout hold of 7-14 days is Stripe-documented; KYC itself has no
  published SLA. Notarization and Apple org enrollment both show forum-documented
  tails (72h+ and 75+ days respectively) far beyond their official medians — the two
  buffer items in the plan.
- Arc stretched 7→8 weeks: the old plan sequenced distribution at week 3 assuming
  same-day everything; the documented latencies make week 4 honest with week-one
  account-opening.

## Version history

- **v2** — latency evidence throughout: payments moved ahead of legal (start-early
  latency floor), distribution week 3→4, arc 7→8 weeks, concrete policy-trap examples
  in `policy_check`, generator-vs-lawyer guidance and auto-rejection vectors in
  `legal_review`.
- **v1** — initial arc.
