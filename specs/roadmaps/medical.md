---
id: medical
version: 1
title: Get into medical school and through the first two years
horizon: annual
definition_of_done: Matched into a residency-track programme with Step 1 passed.
---

# Medical path

The only roadmap here that spans years rather than quarters — roughly four, from
prerequisites to match. The horizon is still annual because the goal engine reviews it
annually; the offsets carry the real span.

## Steps

```json
{
  "steps": [
    {
      "key": "prereqs",
      "title": "Finish prerequisite coursework and clinical hours",
      "offset_weeks": 0,
      "detail": "Bio, chem, orgo, physics, biochem, plus documented clinical exposure."
    },
    {
      "key": "mcat_prep",
      "title": "MCAT preparation block",
      "offset_weeks": 30,
      "detail": "Content review first, then full-length practice under real timing.",
      "cadences": ["practice_sections"]
    },
    {
      "key": "mcat",
      "title": "Sit the MCAT",
      "offset_weeks": 52
    },
    {
      "key": "primaries",
      "title": "AMCAS primary application submitted",
      "offset_weeks": 62,
      "detail": "Rolling admissions: submitting in June beats a better essay in September."
    },
    {
      "key": "secondaries",
      "title": "Secondary essays returned within two weeks each",
      "offset_weeks": 70,
      "cadences": ["secondary_essays"]
    },
    {
      "key": "interviews",
      "title": "Medical school interviews",
      "offset_weeks": 82
    },
    {
      "key": "step1",
      "title": "Pass Step 1",
      "offset_weeks": 180,
      "detail": "Two preclinical years sit between the acceptance and this."
    },
    {
      "key": "match",
      "title": "Match into a residency-track programme",
      "offset_weeks": 208
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
      "title": "Secondary essays drafted",
      "weekly_count": 4,
      "estimated_minutes_each": 75
    }
  ]
}
```
