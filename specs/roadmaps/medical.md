---
id: medical
version: 3
title: Get into medical school and through the first two years
horizon: annual
signals: mcat, premed, shadowing, clinical, volunteer, hospice, anki
definition_of_done: Matriculated at an MD programme with Step 1 passed.
---

# Medical path

The only roadmap here that spans years rather than quarters — roughly four, from
prerequisites through the preclinical boards. The horizon is still annual because the
goal engine reviews it annually; the offsets carry the real span.

Version 3 rebuilt the arc against current admissions evidence (AAMC 2024–2026 data,
Shemmassian/MedEdits/Med School Insiders timelines). Two structural changes: an
`app_assets` step now covers the letters/personal-statement/school-list work the old
preset silently assumed would happen, and the hour totals moved from
"average-accepted" defaults to the top-tier-recommended band. Offsets encode relative
spacing (prep starts 22 weeks before the sit; the primary goes in four weeks after);
the instantiation interview pins them to the real calendar. The immovable dates live
under "Calendar anchors" below.

## Steps

```json
{
  "steps": [
    {
      "key": "prereqs",
      "title": "Prerequisite coursework and longitudinal extracurricular base",
      "offset_weeks": 0,
      "detail": "Bio, chem, orgo, physics, biochem. Start every hour category now: adcoms weight sustained multi-year commitment in one role over totals assembled late. One clinical role, one lab, one service line, each held a year or more, beats a dozen short stints."
    },
    {
      "key": "mcat_prep",
      "title": "MCAT preparation block",
      "offset_weeks": 30,
      "detail": "22 weeks before the sit: 15-20 h/week alongside work. Content review first; reserve the six official AAMC full-lengths for the final six weeks, one per week, each followed by a full review day. Target band for top-tier programmes is 518-523 (matriculant medians).",
      "cadences": ["practice_sections"]
    },
    {
      "key": "app_assets",
      "title": "Letters, personal statement, school list, SJTs",
      "offset_weeks": 34,
      "detail": "Runs parallel to MCAT prep. Request 4-6 letters (two science faculty minimum; committee letter if offered) so all are submitted by end of June. Personal statement drafting starts here — six months of revision cycles before submission, one narrative throughline that the activities section and secondaries will reinforce. Build the school list from MSAR medians, not rankings. Register for Casper/PREview per each school's stance; sit them May-June."
    },
    {
      "key": "mcat",
      "title": "Sit the MCAT",
      "offset_weeks": 52,
      "detail": "Late April preserves four essay-focused weeks before submission; the latest safe sit for a June primary is late May. Scores return in 30-35 days. Decide sit/delay from the FL trend line, not hope."
    },
    {
      "key": "primaries",
      "title": "AMCAS primary submitted in the first week",
      "offset_weeks": 56,
      "detail": "AMCAS opens early May, accepts submissions from late May, transmits from late June. Verification takes days for first-week submitters and a month-plus once the summer queue builds — under rolling admissions, submitting in the first week beats a better essay in September."
    },
    {
      "key": "secondaries",
      "title": "Secondary essays returned within two weeks each",
      "offset_weeks": 60,
      "detail": "Prewrite from prior-year prompts starting the week the primary goes in — schools reuse roughly three-quarters of prompts. Fourteen days per school is the ceiling; banked prewrites make peak season (July-August) survivable.",
      "cadences": ["secondary_essays"]
    },
    {
      "key": "interviews",
      "title": "Interview season",
      "offset_weeks": 68,
      "detail": "Invites from August; season runs September-March. Prep both formats — MMI stations and traditional — with mocks from the first invite. First acceptances land October 15. Hold multiple offers until April 30, then narrow to one; a letter of intent goes to exactly one school, and only if waitlisted at the top choice.",
      "cadences": ["mock_interviews"]
    },
    {
      "key": "step1",
      "title": "Pass Step 1",
      "offset_weeks": 204,
      "detail": "Matriculation ~week 108; the two preclinical years end here. Pass/fail since 2022 — the goal is a comfortable first-attempt pass, not a score."
    }
  ],
  "cadences": [
    {
      "key": "practice_sections",
      "title": "MCAT practice sections",
      "weekly_count": 5,
      "estimated_minutes_each": 95
    },
    {
      "key": "secondary_essays",
      "title": "Secondary essays drafted or polished",
      "weekly_count": 7,
      "estimated_minutes_each": 60
    },
    {
      "key": "mock_interviews",
      "title": "Mock interviews (MMI and traditional)",
      "weekly_count": 2,
      "estimated_minutes_each": 60
    }
  ],
  "totals": [
    {
      "key": "shadowing",
      "title": "Shadowing hours (3+ specialties, ≥1 primary care)",
      "total_count": 75
    },
    {
      "key": "clinical",
      "title": "Clinical experience hours (paid or volunteer)",
      "total_count": 500
    },
    {
      "key": "volunteering",
      "title": "Non-clinical service hours",
      "total_count": 500
    },
    {
      "key": "research",
      "title": "Research hours",
      "total_count": 1000
    },
    {
      "key": "leadership",
      "title": "Leadership and teaching hours",
      "total_count": 150
    }
  ]
}
```

## Calendar anchors

Offsets are relative; these dates are not. The interview should place `primaries` in
the first week of June and let everything else fall out from there.

| Date | What |
|---|---|
| Early May | AMCAS opens; Casper/PREview windows open |
| Late May | Earliest AMCAS submission; latest safe MCAT sit |
| First week of June | Primary in — verification takes days now, a month-plus by July |
| Late June | AMCAS transmits to schools; letters should all be in |
| July–August | Secondary peak; 14-day SLA per school |
| August–March | Interview invites, then season |
| October 15 | First acceptances (rolling) |
| April 30 | Narrow to one school |

## Hour tiers

Defaults above are the **top-tier-recommended** band (Shemmassian's three-tier
framework, cross-checked against T20 matriculant profiles). Every total is editable
on the roadmap page after instantiation; the floor and middle tiers are what to fall
back to if the calendar wins:

| Category | Floor (avg accepted) | Competitive | Top-tier (default) |
|---|---|---|---|
| Clinical | 100 | 300 | 500 |
| Shadowing | 30 | 50 | 75 — the 75-1-3 rule: one primary-care doc, three specialists |
| Non-clinical service | 100 | 300 | 500 — surveyed adcoms weight this category highest |
| Research | course-based | 400 | 1,000 — publications/posters differentiate; most matriculants have none |
| Leadership | 1 role, 3+ months | 3 roles | 150 h across 3+ roles with measurable impact |

Hours screen; depth ranks. Past the competitive tier, what separates files is
sustained longitudinal commitment, reflection ("distance traveled"), and a coherent
narrative — the personal statement, the three most-meaningful essays (readers skim
the fifteen activities and read those three closely), and the secondaries all telling
one story. Stats context: national matriculant medians run ~511-514 MCAT / 3.79-3.86
GPA; T20 medians run ~518-523 / ~3.9. Most matriculants (74% in 2024) take at least
one gap year — the offsets here assume that shape.

Each logged entry should carry its org and supervisor in the note — that note stream,
plus the activity registry, is the raw material for the AMCAS Work & Activities
section (15 slots, 700 characters each, 3 most-meaningful at 1,325).

## Version history

- **v3** — rebuilt against 2025-2026 cycle evidence: added `app_assets` and
  `mock_interviews`; folded the prep-block framing into `mcat_prep` detail; replaced
  the mislabeled `match` step (week 208 is end of M2, not the NRMP match) with
  `step1` at its true offset; raised totals from average-accepted to top-tier band
  (60/150/100/200/50 → 75/500/500/1000/150); secondary cadence 4×75 → 7×60 on the
  prewriting model.
- **v2** — added the AMCAS-category hour totals.
