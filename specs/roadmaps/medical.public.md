---
id: medical
version: 3
title: Get into medical school and through the first two years
horizon: annual
definition_of_done: Matriculated at an MD program with the first licensing exam passed.
---

# Medical path (sample data)

**This preset is entirely fictional** — a public-repo stand-in for a real applicant's
instantiated roadmap, built to exercise the goal engine's longest-horizon case (offsets
spanning roughly four years, five lifetime hour totals, a personalization interview)
without carrying anyone's real dates, scores, or program list. Treat every number below
as an illustrative placeholder, not admissions advice.

It is also the only preset here that spans years rather than quarters. The horizon
field stays `annual` because the goal engine reviews every roadmap on an annual cadence
regardless of how far its offsets run — the field describes the review rhythm, not the
roadmap's total length.

## Steps

```json
{
  "steps": [
    {
      "key": "prereqs",
      "title": "Prerequisite coursework and a longitudinal activity base",
      "offset_weeks": 0,
      "detail": "Core science coursework plus one clinical role, one research or lab role, and one sustained service commitment, each held a year or more. Sustained depth in a few lanes reads better on an application than a wide scatter of short stints."
    },
    {
      "key": "mcat_prep",
      "title": "Entrance-exam preparation block",
      "offset_weeks": 30,
      "detail": "A dedicated multi-month prep block layered on top of the ongoing activities above, not instead of them. Content review first, full-length practice exams reserved for the final stretch, each followed by a review pass before the next one.",
      "cadences": ["practice_sections"]
    },
    {
      "key": "app_assets",
      "title": "Letters, personal statement, and program list",
      "offset_weeks": 34,
      "detail": "Runs alongside exam prep, not after it. Request letters of recommendation early enough that every writer has months, not weeks. Draft the personal statement across several revision passes with outside readers, and build the program list from published median statistics for each program rather than a general reputation ranking."
    },
    {
      "key": "mcat",
      "title": "Sit the entrance exam",
      "offset_weeks": 52,
      "detail": "Timed to leave a real buffer before the application opens, with enough runway afterward to still submit in the earliest window if the score needs a retake to hit the target band. Decide sit-or-delay from the practice-exam trend line, not a hopeful guess."
    },
    {
      "key": "primaries",
      "title": "Primary application submitted in the first eligible week",
      "offset_weeks": 56,
      "detail": "Centralized application services process earliest submitters fastest and slow down considerably once the summer queue builds — under a rolling review model, an early-but-good application consistently beats a polished late one."
    },
    {
      "key": "secondaries",
      "title": "Program-specific secondary essays turned around quickly",
      "offset_weeks": 60,
      "detail": "Many secondary prompts repeat year over year, so drafting generic answers to the common prompt themes before they arrive turns a two-week scramble per program into a same-day turnaround during the peak season.",
      "cadences": ["secondary_essays"]
    },
    {
      "key": "interviews",
      "title": "Interview season",
      "offset_weeks": 68,
      "detail": "Interview formats vary by program — panel, one-on-one, and multi-station formats all show up — so mock sessions should cover more than one format well before the first real invite. Hold multiple offers open only as long as fairness to other applicants allows, then commit.",
      "cadences": ["mock_interviews"]
    },
    {
      "key": "step1",
      "title": "Pass the first licensing exam",
      "offset_weeks": 204,
      "detail": "Lands at the end of the preclinical years, roughly two years after matriculation. Scored pass/fail on this exam's current format — the goal is a comfortable first-attempt pass, not chasing a numeric score that no longer exists on the score report."
    }
  ],
  "cadences": [
    {
      "key": "practice_sections",
      "title": "Timed practice sections",
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
      "title": "Mock interviews",
      "weekly_count": 2,
      "estimated_minutes_each": 60
    }
  ],
  "totals": [
    {
      "key": "shadowing",
      "title": "Shadowing hours (multiple specialties, at least one primary care)",
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

## Sequencing notes

Offsets are relative, not absolute — the instantiation interview pins `primaries` to a
real calendar week and everything else falls out from there. A few sequencing facts
worth stating plainly:

- `mcat_prep` and `app_assets` run in parallel, not in sequence — trying to finish one
  before starting the other is what pushes the whole arc past a year.
- `secondaries` starts its prewriting cadence the same week `primaries` goes in, well
  before any actual secondary prompt arrives, because the peak-season volume only
  survives on banked drafts.
- `step1` sits nearly two academic years after everything before it — the long tail this
  preset exists to exercise in the goal engine's risk-window math.

## Hour bands (illustrative only)

The totals above sit at what admissions guidance commonly frames as a strong,
well-rounded band rather than a bare minimum. Every total is editable on the roadmap
page after instantiation — these are starting points, not requirements:

| Category | Lighter | Typical | Used here |
|---|---|---|---|
| Clinical | 100 | 300 | 500 |
| Shadowing | 30 | 50 | 75 |
| Non-clinical service | 100 | 300 | 500 |
| Research | course-based | 400 | 1,000 |
| Leadership | one role | a few roles | 150 across several roles |

Hours are a screen, not the whole file — sustained commitment in a few lanes, reflected
on rather than just logged, is what the numbers are a proxy for. National matriculant
medians for the entrance exam and GPA run in the low-to-mid range of the possible
scale; the offsets above assume most applicants take at least one gap year between
finishing prerequisites and matriculating, which is now the majority path rather than
the exception.

## Version history

- **v3** — sample data rebuilt for the public preset: five lifetime hour totals across
  the categories above, a parallel exam-prep/application-assets track, and an explicit
  long-tail `step1` milestone to exercise multi-year risk-window math.
- **v2** — added the category hour totals.
