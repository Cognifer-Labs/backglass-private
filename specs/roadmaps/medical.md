---
id: medical
version: 2
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
  ],
  "totals": [
    {
      "key": "shadowing",
      "title": "Shadowing hours",
      "total_count": 60
    },
    {
      "key": "clinical",
      "title": "Clinical experience hours (paid or volunteer)",
      "total_count": 150
    },
    {
      "key": "volunteering",
      "title": "Non-clinical volunteering hours",
      "total_count": 100
    },
    {
      "key": "research",
      "title": "Research hours",
      "total_count": 200
    },
    {
      "key": "leadership",
      "title": "Leadership and teaching hours",
      "total_count": 50
    }
  ]
}
```

Version 2 added the AMCAS-category hour totals. Defaults follow common admissions
guidance (shadowing 60, clinical 150, non-clinical 100, research 200, leadership 50);
every one is editable on the roadmap page after instantiation, and each logged entry
should carry its org and supervisor in the note — that note stream is the raw material
for the Work & Activities section later.
